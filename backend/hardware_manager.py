"""Hardware profiler + compute scheduler.

Four modes:
  auto    – profile the PC and pick GPU / hybrid layer-split / CPU-only automatically
  gpu     – force every supported workload onto the GPU (n_gpu_layers = -1)
  cpu     – CUDA_VISIBLE_DEVICES="" and n_gpu_layers = 0 everywhere
  hybrid  – split transformer layers between VRAM and RAM *and* run GPU + CPU task pools in parallel

The resulting ComputePlan is consumed by every adapter (LLM options, embedding device,
humanizer device, worker pools) so the whole pipeline follows one decision.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

try:
    import psutil
except ImportError:  # psutil is in requirements; keep the module importable regardless
    psutil = None

MODES = ("auto", "gpu", "cpu", "hybrid")
MODE_LABELS = {"auto": "Auto (Recommended)", "gpu": "GPU Only", "cpu": "CPU Only", "hybrid": "Hybrid (CPU + GPU)"}
INTEGRATED_PAT = re.compile(r"(Intel\(R\)\s*(UHD|HD|Iris)|Iris Xe|Radeon\(TM\)\s*Graphics|Radeon Vega|Microsoft Basic)", re.I)
OOM_PAT = re.compile(r"(out of memory|cudaMalloc|CUDA error|OOM|failed to allocate|insufficient memory)", re.I)
VRAM_RESERVE_GB = 1.0          # KV cache / context / driver headroom kept free on each GPU
GOOD_GPU_VRAM_GB = 8.0


@dataclass
class GPUInfo:
    index: int
    name: str
    vendor: str
    backend: str               # cuda | rocm | directml
    vram_total_gb: float
    vram_free_gb: float
    integrated: bool = False
    utilization: float = 0.0


@dataclass
class HardwareProfile:
    os: str
    cpu_model: str
    physical_cores: int
    logical_cores: int
    ram_total_gb: float
    ram_available_gb: float
    avx2: bool
    avx512: bool
    gpus: list[GPUInfo] = field(default_factory=list)
    torch_cuda: bool = False
    probed_at: float = 0.0

    @property
    def discrete_gpus(self) -> list[GPUInfo]:
        return [g for g in self.gpus if not g.integrated]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["discrete_gpu_count"] = len(self.discrete_gpus)
        return d


# ---------------------------------------------------------------- probing
def _run(cmd: list[str], timeout: float = 6.0) -> str:
    try:
        flags = 0x08000000 if sys.platform.startswith("win") else 0  # CREATE_NO_WINDOW
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags)
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _cpu_model() -> str:
    if sys.platform.startswith("win"):
        out = _run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"])
        if out.strip():
            return out.strip().splitlines()[0]
    try:
        for line in open("/proc/cpuinfo", encoding="utf-8"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def _simd_flags() -> tuple[bool, bool]:
    if sys.platform.startswith("win"):
        try:
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            # PF_AVX2_INSTRUCTIONS_AVAILABLE = 40, PF_AVX512F_INSTRUCTIONS_AVAILABLE = 41
            return bool(k32.IsProcessorFeaturePresent(40)), bool(k32.IsProcessorFeaturePresent(41))
        except Exception:
            return True, False
    try:
        flags = open("/proc/cpuinfo", encoding="utf-8").read()
        return (" avx2" in flags), (" avx512f" in flags)
    except OSError:
        return False, False


def _nvidia_gpus() -> list[GPUInfo]:
    smi = shutil.which("nvidia-smi")
    if not smi and sys.platform.startswith("win"):
        cand = r"C:\Windows\System32\nvidia-smi.exe"
        smi = cand if os.path.exists(cand) else None
    if not smi:
        return []
    out = _run([smi, "--query-gpu=index,name,memory.total,memory.free,utilization.gpu",
                "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            try:
                gpus.append(GPUInfo(int(parts[0]), parts[1], "NVIDIA", "cuda",
                                    round(float(parts[2]) / 1024, 2), round(float(parts[3]) / 1024, 2),
                                    False, float(parts[4] or 0)))
            except ValueError:
                continue
    return gpus


def _windows_other_gpus(start: int) -> list[GPUInfo]:
    if not sys.platform.startswith("win"):
        return []
    # qwMemorySize in the registry is 64-bit (AdapterRAM caps at 4 GB), so read both.
    ps = ("Get-CimInstance Win32_VideoController | ForEach-Object { "
          "[pscustomobject]@{Name=$_.Name; Ram=$_.AdapterRAM} } | ConvertTo-Json -Compress")
    raw = _run(["powershell", "-NoProfile", "-Command", ps])
    try:
        items = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError:
        return []
    if isinstance(items, dict):
        items = [items]
    gpus, idx = [], start
    for it in items:
        name = (it.get("Name") or "").strip()
        if not name or "NVIDIA" in name.upper():
            continue
        vendor = "AMD" if re.search(r"AMD|Radeon", name, re.I) else "Intel" if "Intel" in name else "Other"
        gb = round((it.get("Ram") or 0) / 1024 ** 3, 2)
        integrated = bool(INTEGRATED_PAT.search(name))
        gpus.append(GPUInfo(idx, name, vendor, "directml" if vendor != "AMD" or integrated else "rocm",
                            gb, gb, integrated))
        idx += 1
    return gpus


def probe_hardware() -> HardwareProfile:
    """Real probe of the host PC. Can be replaced by RESEARCHMATE_MOCK_HW (JSON) for testing."""
    mock = os.environ.get("RESEARCHMATE_MOCK_HW")
    if mock:
        return profile_from_dict(json.loads(mock))
    avx2, avx512 = _simd_flags()
    if psutil:
        vm = psutil.virtual_memory()
        phys = psutil.cpu_count(logical=False) or os.cpu_count() or 1
        logi = psutil.cpu_count(logical=True) or phys
        ram_t, ram_a = vm.total / 1024 ** 3, vm.available / 1024 ** 3
    else:
        phys = logi = os.cpu_count() or 1
        ram_t = ram_a = 0.0
    gpus = _nvidia_gpus()
    gpus += _windows_other_gpus(len(gpus))
    torch_cuda = False
    try:
        import torch  # optional
        torch_cuda = bool(torch.cuda.is_available())
    except Exception:
        pass
    return HardwareProfile(f"{platform.system()} {platform.release()}", _cpu_model(), phys, logi,
                           round(ram_t, 2), round(ram_a, 2), avx2, avx512, gpus, torch_cuda, time.time())


def profile_from_dict(d: dict) -> HardwareProfile:
    gpus = [GPUInfo(**{k: v for k, v in g.items() if k in GPUInfo.__dataclass_fields__}) for g in d.get("gpus", [])]
    return HardwareProfile(d.get("os", "Windows 11"), d.get("cpu_model", "Mock CPU"),
                           int(d.get("physical_cores", 8)), int(d.get("logical_cores", 16)),
                           float(d.get("ram_total_gb", 16)), float(d.get("ram_available_gb", 12)),
                           bool(d.get("avx2", True)), bool(d.get("avx512", False)), gpus,
                           bool(d.get("torch_cuda", False)), time.time())


# ---------------------------------------------------------------- planning
def layers_that_fit(free_vram_gb: float, model_size_gb: float, total_layers: int) -> int:
    """Transformer layers that fit in free VRAM after reserving KV-cache headroom."""
    if model_size_gb <= 0 or total_layers <= 0:
        return 0
    per_layer = model_size_gb / total_layers
    usable = max(0.0, free_vram_gb - VRAM_RESERVE_GB)
    return max(0, min(total_layers, int(math.floor(usable / per_layer))))


def build_plan(mode: str, prof: HardwareProfile, *, model_size_gb: float = 4.9, total_layers: int = 32,
               manual_gpu_layers: int | None = None) -> dict:
    mode = mode if mode in MODES else "auto"
    threads = max(1, prof.physical_cores - 1)
    discrete = sorted(prof.discrete_gpus, key=lambda g: g.vram_free_gb, reverse=True)
    total_free = sum(g.vram_free_gb for g in discrete)
    warnings: list[str] = []
    simd = "AVX-512" if prof.avx512 else "AVX2" if prof.avx2 else "SSE"

    def tensor_split(gs: list[GPUInfo]) -> list[float]:
        tot = sum(max(g.vram_free_gb, 0.01) for g in gs)
        return [round(max(g.vram_free_gb, 0.01) / tot, 3) for g in gs]

    def cpu_plan(reason: str) -> dict:
        return dict(effective="cpu", n_gpu_layers=0, num_thread=threads, cuda_visible_devices="",
                    embed_device="cpu", humanizer_device="cpu", gpu_workers=0, cpu_workers=threads,
                    tensor_split=[], main_gpu=None,
                    label=f"CPU Only [{simd} · {threads} threads]", reason=reason)

    if mode == "cpu":
        plan = cpu_plan("User forced CPU Only — GPU left free for other applications.")
    elif mode == "gpu":
        if not discrete:
            plan = cpu_plan("GPU Only requested but no discrete GPU found — falling back to CPU.")
            warnings.append("No discrete GPU detected; GPU Only cannot be honoured.")
        else:
            if model_size_gb + VRAM_RESERVE_GB > total_free:
                warnings.append(f"VRAM guard: model needs ~{model_size_gb + VRAM_RESERVE_GB:.1f} GB but only "
                                f"{total_free:.1f} GB VRAM is free — expect OOM; Hybrid is safer.")
            plan = dict(effective="gpu", n_gpu_layers=-1, num_thread=threads, cuda_visible_devices=",".join(
                str(g.index) for g in discrete if g.backend == "cuda"), embed_device=_dev(discrete[0], 0),
                humanizer_device=_dev(discrete[0], 0), gpu_workers=len(discrete), cpu_workers=1,
                tensor_split=tensor_split(discrete) if len(discrete) > 1 else [], main_gpu=discrete[0].index,
                label=f"GPU Only [{discrete[0].name}]" + (f" +{len(discrete) - 1} GPU" if len(discrete) > 1 else ""),
                reason="User forced GPU Only — all supported workloads on GPU.")
    else:
        # auto + hybrid share the layer-split maths
        if not discrete:
            plan = cpu_plan("No discrete GPU / integrated graphics only → multi-threaded CPU with quantised models.")
            if mode == "hybrid":
                warnings.append("Hybrid needs a discrete GPU; running CPU-only.")
        else:
            fit = layers_that_fit(total_free, model_size_gb, total_layers)
            mismatched = len(discrete) > 1 and (max(g.vram_total_gb for g in discrete)
                                                - min(g.vram_total_gb for g in discrete)) >= 2
            big = discrete[0].vram_total_gb >= GOOD_GPU_VRAM_GB and fit >= total_layers
            if mode == "auto" and big and not mismatched:
                second = discrete[1] if len(discrete) > 1 else None
                plan = dict(effective="gpu", n_gpu_layers=-1, num_thread=threads,
                            cuda_visible_devices=",".join(str(g.index) for g in discrete if g.backend == "cuda"),
                            embed_device=_dev(second, 1) if second else "cpu",
                            humanizer_device=_dev(second, 1) if second else "cpu",
                            gpu_workers=1, cpu_workers=threads, tensor_split=[], main_gpu=discrete[0].index,
                            label=f"Auto → GPU [{discrete[0].name}] + {threads} CPU threads",
                            reason="GPU ≥ 8 GB VRAM: LLM fully on GPU; embeddings/GROBID/linting on "
                                   + ("second GPU + CPU." if second else "CPU worker threads."))
            else:
                layers = fit if manual_gpu_layers is None else max(0, min(total_layers, manual_gpu_layers))
                why = ("Multi-GPU with mismatched VRAM" if mismatched else
                       "Limited VRAM" if fit < total_layers else "Hybrid requested")
                plan = dict(effective="hybrid", n_gpu_layers=layers, num_thread=threads,
                            cuda_visible_devices=",".join(str(g.index) for g in discrete if g.backend == "cuda"),
                            embed_device="cpu", humanizer_device="cpu", gpu_workers=1, cpu_workers=threads,
                            tensor_split=tensor_split(discrete) if len(discrete) > 1 else [],
                            main_gpu=discrete[0].index,
                            label=("Auto → " if mode == "auto" else "") +
                                  f"Hybrid [{discrete[0].name} {layers}/{total_layers} layers + {threads} CPU threads]",
                            reason=f"{why}: {layers} of {total_layers} layers in VRAM, remaining layers + KV cache "
                                   f"in system RAM; drafting on GPU while PDF indexing, GROBID, linting and "
                                   f"RefChecker run on CPU workers.")
    if mode == "auto" and not plan["label"].startswith("Auto"):
        plan["label"] = "Auto → " + plan["label"]
    plan.update(requested=mode, requested_label=MODE_LABELS[mode], warnings=warnings,
                model_size_gb=model_size_gb, total_layers=total_layers)
    return plan


def _dev(g: GPUInfo | None, fallback_idx: int) -> str:
    if g is None:
        return "cpu"
    return f"cuda:{g.index}" if g.backend == "cuda" else "dml" if g.backend == "directml" else f"cuda:{fallback_idx}"


def plan_env(plan: dict) -> dict:
    """Environment variables child processes (ollama, llama.cpp, torch) should see."""
    env = {"OMP_NUM_THREADS": str(plan["num_thread"])}
    if plan["effective"] == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = ""
    elif plan.get("cuda_visible_devices"):
        env["CUDA_VISIBLE_DEVICES"] = plan["cuda_visible_devices"]
    return env


def llm_options(plan: dict) -> dict:
    """Ollama /api/generate `options` (llama.cpp uses the same numbers as n_gpu_layers / threads)."""
    opts: dict[str, Any] = {"num_thread": plan["num_thread"]}
    opts["num_gpu"] = 999 if plan["n_gpu_layers"] == -1 else plan["n_gpu_layers"]
    if plan.get("main_gpu") is not None:
        opts["main_gpu"] = plan["main_gpu"]
    return opts


def run_with_oom_fallback(fn: Callable[[dict], Any], plan: dict, events: list | None = None) -> Any:
    """Call fn(plan); on an OOM-looking error halve GPU layers and retry until CPU-only."""
    current = dict(plan)
    while True:
        try:
            return fn(current)
        except Exception as exc:  # noqa: BLE001 — we only swallow OOM-looking errors
            if not OOM_PAT.search(str(exc)) or current["n_gpu_layers"] == 0:
                raise
            layers = current.get("total_layers", 32) if current["n_gpu_layers"] == -1 else current["n_gpu_layers"]
            new = layers // 2
            if events is not None:
                events.append({"from_layers": "all" if current["n_gpu_layers"] == -1 else current["n_gpu_layers"],
                               "to_layers": new, "error": str(exc)[:200]})
            current = dict(current, n_gpu_layers=new, effective="hybrid" if new else "cpu",
                           label=f"OOM fallback → {new} GPU layers")


# ---------------------------------------------------------------- manager / scheduler
class HardwareManager:
    def __init__(self, probe: Callable[[], HardwareProfile] = probe_hardware):
        self._probe = probe
        self._lock = threading.RLock()
        self.profile: HardwareProfile = probe()
        self.mode = "auto"
        self.model_size_gb = 4.9
        self.total_layers = 32
        self.manual_gpu_layers: int | None = None
        self.oom_events: list[dict] = []
        self._tele_cache: tuple[float, dict] = (0.0, {})
        self.plan = self._compute()
        self._pools()

    def _compute(self) -> dict:
        return build_plan(self.mode, self.profile, model_size_gb=self.model_size_gb,
                          total_layers=self.total_layers, manual_gpu_layers=self.manual_gpu_layers)

    def _pools(self) -> None:
        self.cpu_pool = ThreadPoolExecutor(max_workers=max(2, min(8, self.plan["cpu_workers"])),
                                           thread_name_prefix="rm-cpu")
        self.gpu_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rm-gpu")

    def set_mode(self, mode: str, *, model_size_gb: float | None = None, total_layers: int | None = None,
                 manual_gpu_layers: int | None = None, reprobe: bool = False) -> dict:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        with self._lock:
            if reprobe:
                self.profile = self._probe()
            self.mode = mode
            if model_size_gb:
                self.model_size_gb = float(model_size_gb)
            if total_layers:
                self.total_layers = int(total_layers)
            self.manual_gpu_layers = manual_gpu_layers
            self.plan = self._compute()
            os.environ.update({k: v for k, v in plan_env(self.plan).items()})
            return self.plan

    def submit(self, kind: str, fn: Callable, *args, **kwargs) -> Future:
        """kind='gpu' tasks go to the single GPU lane unless CPU mode; everything else to CPU workers."""
        pool = self.gpu_pool if kind == "gpu" and self.plan["effective"] != "cpu" else self.cpu_pool
        return pool.submit(fn, *args, **kwargs)

    def run_parallel(self, gpu_tasks: dict[str, Callable], cpu_tasks: dict[str, Callable]) -> dict:
        futs = {k: self.submit("gpu", f) for k, f in gpu_tasks.items()}
        futs.update({k: self.submit("cpu", f) for k, f in cpu_tasks.items()})
        out = {}
        for k, f in futs.items():
            try:
                out[k] = {"ok": True, "result": f.result()}
            except Exception as exc:  # noqa: BLE001
                out[k] = {"ok": False, "error": str(exc)}
        return out

    def telemetry(self) -> dict:
        now = time.time()
        if now - self._tele_cache[0] < 1.5:
            return self._tele_cache[1]
        t: dict[str, Any] = {"cpu_percent": 0.0, "ram_used_gb": 0.0, "ram_total_gb": self.profile.ram_total_gb,
                             "gpus": []}
        if psutil:
            t["cpu_percent"] = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            t["ram_used_gb"] = round((vm.total - vm.available) / 1024 ** 3, 2)
            t["ram_percent"] = vm.percent
        if not os.environ.get("RESEARCHMATE_MOCK_HW"):
            for g in _nvidia_gpus():
                t["gpus"].append({"index": g.index, "name": g.name, "util": g.utilization,
                                  "vram_used_gb": round(g.vram_total_gb - g.vram_free_gb, 2),
                                  "vram_total_gb": g.vram_total_gb})
        else:
            t["gpus"] = [{"index": g.index, "name": g.name, "util": g.utilization,
                          "vram_used_gb": round(g.vram_total_gb - g.vram_free_gb, 2),
                          "vram_total_gb": g.vram_total_gb} for g in self.profile.discrete_gpus]
        t["mode"] = self.mode
        t["label"] = self.plan["label"]
        self._tele_cache = (now, t)
        return t

    def snapshot(self) -> dict:
        return {"profile": self.profile.to_dict(), "mode": self.mode, "modes": MODE_LABELS,
                "plan": self.plan, "llm_options": llm_options(self.plan), "env": plan_env(self.plan),
                "oom_events": self.oom_events[-10:]}
