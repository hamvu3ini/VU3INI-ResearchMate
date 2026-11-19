"""Automatic local-LLM provisioning for Step 4.

If the PC has no working Ollama + llama3.1:8b, ResearchMate does it for the user:
  1. install   – vendor/installers/OllamaSetup.exe (offline) or download it from ollama.com (online),
                 run silently per-user (no admin): %LOCALAPPDATA%\\Programs\\Ollama\\ollama.exe
  2. serve     – start `ollama serve` in the background (GPU/CPU env from the compute plan)
  3. model     – `ollama pull llama3.1:8b` (online) or, offline, import the vendored GGUF with `ollama create`
Runs as a background job; the browser polls /api/llm/setup/status for a live progress bar.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

import httpx

from .. import config
from ..netstatus import is_online

DEFAULT_MODEL = "llama3.1:8b"
OLLAMA_URL = "http://127.0.0.1:11434"
INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"
NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0
DETACHED = 0x00000008 if sys.platform.startswith("win") else 0


def model_present(models: list[str], wanted: str = DEFAULT_MODEL) -> bool:
    base = wanted.split(":")[0]
    aliases = {wanted, f"{wanted}-instruct"} | ({f"{base}:latest"} if wanted.endswith(":8b") else set())
    return any(m in aliases or m.startswith(wanted) for m in models)


class OllamaSetup:
    def __init__(self, env_provider: Callable[[], dict] | None = None):
        self.env_provider = env_provider or (lambda: {})
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.job: dict = {"stage": "idle", "progress": 0.0, "message": "", "error": None, "done": False,
                          "model": DEFAULT_MODEL, "log": []}

    # ---------------------------------------------------------------- probes (patched in tests)
    def find_exe(self) -> str | None:
        """An Ollama the user installed (official build) wins; otherwise the copy bundled in vendor\\bin\\tools."""
        if sys.platform.startswith("win"):
            for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
                if not base:
                    continue
                for cand in (Path(base) / "Programs" / "Ollama" / "ollama.exe", Path(base) / "Ollama" / "ollama.exe"):
                    if cand.exists():
                        return str(cand)
        tools = {str(d) for d in config.vendored_tool_dirs()}
        on_path = shutil.which("ollama")
        if on_path and str(Path(on_path).parent) not in tools:
            return on_path
        return config.find_binary("ollama")

    def is_bundled(self, exe: str | None) -> bool:
        return bool(exe) and str(config.VENDOR_DIR) in str(exe)

    def is_running(self) -> bool:
        try:
            return httpx.get(OLLAMA_URL + "/api/tags", timeout=1.5).status_code == 200
        except httpx.HTTPError:
            return False

    def models(self) -> list[str]:
        try:
            return [m["name"] for m in httpx.get(OLLAMA_URL + "/api/tags", timeout=3).json().get("models", [])]
        except (httpx.HTTPError, ValueError):
            return []

    def vendored_installer(self) -> Path | None:
        p = config.VENDOR_DIR / "installers" / "OllamaSetup.exe"
        return p if p.exists() else None

    def vendored_gguf(self) -> Path | None:
        d = config.VENDOR_DIR / "models" / "ollama"
        if d.exists():
            for p in sorted(d.glob("*.gguf")):
                return p
        return None

    def status(self) -> dict:
        exe = self.find_exe()
        running = self.is_running()
        models = self.models() if running else []
        return {"installed": bool(exe), "binary": exe, "bundled": self.is_bundled(exe),
                "gpu_runner": bool(config.find_binary("llama-server")), "running": running, "models": models,
                "default_model": DEFAULT_MODEL, "has_default_model": model_present(models),
                "ready": running and model_present(models), "platform": sys.platform,
                "vendored_installer": bool(self.vendored_installer()), "vendored_gguf": bool(self.vendored_gguf()),
                "job": dict(self.job, log=self.job["log"][-12:])}

    # ---------------------------------------------------------------- actions (patched in tests)
    def install(self, progress: Callable[[float, str], None]) -> str:
        if not sys.platform.startswith("win"):
            raise RuntimeError("Automatic Ollama install is only supported on Windows. Install it from https://ollama.com.")
        installer = self.vendored_installer()
        if not installer:
            if not is_online():
                raise RuntimeError("Ollama is not installed, no installer is bundled in vendor\\installers and the PC "
                                   "is offline. Connect to the internet or rebuild the bundle without --skip-ml.")
            dl = config.data_dir() / "downloads"
            dl.mkdir(parents=True, exist_ok=True)
            installer = dl / "OllamaSetup.exe"
            self._download(INSTALLER_URL, installer, progress)
        progress(0.0, "Installing Ollama silently (per-user, no admin rights needed)…")
        proc = subprocess.run([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"],
                              timeout=900, creationflags=NO_WINDOW)
        if proc.returncode != 0:
            raise RuntimeError(f"Ollama installer exited with code {proc.returncode}.")
        for _ in range(30):
            exe = self.find_exe()
            if exe:
                return exe
            time.sleep(1)
        raise RuntimeError("Ollama installer finished but ollama.exe was not found.")

    def start_serve(self, exe: str) -> None:
        env = dict(os.environ, OLLAMA_HOST="127.0.0.1:11434", **self.env_provider())
        subprocess.Popen([exe, "serve"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=NO_WINDOW | DETACHED, close_fds=True)

    def pull(self, model: str, progress: Callable[[float, str], None]) -> None:
        with httpx.stream("POST", OLLAMA_URL + "/api/pull", json={"model": model, "stream": True},
                          timeout=httpx.Timeout(30, read=None)) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"ollama pull failed: {r.read().decode()[:200]}")
            for line in r.iter_lines():
                if not line.strip():
                    continue
                ev = json.loads(line)
                if ev.get("error"):
                    raise RuntimeError(ev["error"])
                total, done = ev.get("total") or 0, ev.get("completed") or 0
                progress(done / total if total else -1, f"{ev.get('status', 'pulling')}"
                         + (f" — {done / 1e9:.2f} / {total / 1e9:.2f} GB" if total else ""))

    def import_gguf(self, exe: str, gguf: Path, model: str, progress: Callable[[float, str], None]) -> None:
        progress(-1, f"Importing bundled {gguf.name} into Ollama as {model} (offline)…")
        with tempfile.TemporaryDirectory() as td:
            mf = Path(td) / "Modelfile"
            mf.write_text(f'FROM "{gguf.resolve()}"\nPARAMETER temperature 0.6\n', encoding="utf-8")
            proc = subprocess.run([exe, "create", model, "-f", str(mf)], capture_output=True, text=True,
                                  timeout=1800, creationflags=NO_WINDOW)
            if proc.returncode != 0:
                raise RuntimeError(f"ollama create failed: {(proc.stderr or proc.stdout)[:300]}")

    @staticmethod
    def _download(url: str, dest: Path, progress: Callable[[float, str], None]) -> None:
        tmp = dest.with_suffix(".part")
        req = urllib.request.Request(url, headers={"User-Agent": "VU3INI-ResearchMate/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as fh:
            total = int(r.headers.get("content-length") or 0)
            done = 0
            while chunk := r.read(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                progress(done / total if total else -1, f"Downloading Ollama installer — {done / 1e6:.0f} MB")
        tmp.replace(dest)

    # ---------------------------------------------------------------- job orchestration
    def _set(self, stage: str | None = None, progress: float | None = None, message: str | None = None) -> None:
        if stage:
            self.job["stage"] = stage
        if progress is not None:
            self.job["progress"] = round(progress, 4)
        if message:
            self.job["message"] = message
            if not self.job["log"] or self.job["log"][-1] != message:
                self.job["log"].append(message)

    def run(self, model: str = DEFAULT_MODEL) -> dict:
        """Synchronous provisioning. Idempotent: skips anything already done."""
        self.job.update(stage="checking", progress=0.0, message="Checking for Ollama…", error=None, done=False,
                        model=model, log=["Checking for Ollama…"])
        try:
            exe = self.find_exe()
            if not self.is_running():
                if not exe:
                    self._set("installing", 0.0, "Ollama not found — installing it now.")
                    exe = self.install(lambda p, m: self._set(None, p, m))
                    self._set(None, 1.0, f"Ollama installed at {exe}")
                elif self.is_bundled(exe):
                    self._set("installing", 1.0, "Using the Ollama bundled with ResearchMate — nothing to install.")
                self._set("starting", -1, "Starting `ollama serve` in the background…")
                if not self.is_bundled(exe):
                    time.sleep(2)  # the official installer's tray app often starts the server itself
                if not self.is_running():
                    self.start_serve(exe)
                for _ in range(60):
                    if self.is_running():
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("`ollama serve` did not respond on 127.0.0.1:11434 within 60 s.")
            self._set("starting", 1.0, "Ollama server is running.")
            if not model_present(self.models(), model):
                gguf = self.vendored_gguf() if model == DEFAULT_MODEL else None
                if is_online():
                    self._set("pulling", 0.0, f"Running `ollama pull {model}` (about 4.9 GB, first time only)…")
                    self.pull(model, lambda p, m: self._set(None, p, m))
                elif gguf and exe:
                    self._set("pulling", -1, "Offline — importing the bundled model instead of pulling.")
                    self.import_gguf(exe, gguf, model, lambda p, m: self._set(None, p, m))
                else:
                    raise RuntimeError(f"Model {model} is not installed and the PC is offline with no bundled GGUF. "
                                       f"Connect to the internet, or rebuild the bundle without --skip-ml.")
                if not model_present(self.models(), model):
                    raise RuntimeError(f"{model} still not listed by Ollama after download.")
            self._set("ready", 1.0, f"Ready — {model} is available for drafting.")
        except Exception as exc:  # noqa: BLE001 — surface any failure to the browser
            self.job.update(failed_stage=self.job["stage"], stage="error", error=str(exc), message=str(exc))
            self.job["log"].append("ERROR: " + str(exc))
        finally:
            self.job["done"] = True
        return self.status()

    def start(self, model: str = DEFAULT_MODEL) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self.job.update(stage="queued", done=False, error=None, message="Starting setup…", model=model)
            self._thread = threading.Thread(target=self.run, args=(model,), daemon=True, name="ollama-setup")
            self._thread.start()
        return self.status()

    def ensure_server_started(self) -> bool:
        """Called at app start: if Ollama is installed but not running, start it (no install/pull)."""
        if self.is_running():
            return True
        exe = self.find_exe()
        if not exe:
            return False
        self.start_serve(exe)
        return True
