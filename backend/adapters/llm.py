"""Local LLM engine: Ollama (127.0.0.1:11434) and a ResearchMate-managed llama.cpp CUDA server (127.0.0.1:8080).

How the compute plan is honoured:
  CPU Only              → Ollama with num_gpu = 0 (works with the bundled CPU build of Ollama)
  GPU Only / Hybrid /   → if the bundled CUDA llama-server is present, ResearchMate starts it on the GGUF that
  Auto→GPU                Ollama already downloaded (no second copy of the model) with --n-gpu-layers / --threads /
                          --tensor-split from the plan, and restarts it when the plan changes. CUDA out-of-memory
                          during load → layers are halved and the server restarted (run_with_oom_fallback).
                          Without llama-server, Ollama is used with options.num_gpu (needs a GPU-enabled Ollama).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .. import config
from ..hardware_manager import llm_options, plan_env, run_with_oom_fallback
from .base import Adapter

OLLAMA = "http://127.0.0.1:11434"
LLAMACPP_PORT = 8080
LLAMACPP = f"http://127.0.0.1:{LLAMACPP_PORT}"
NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0


class LLMUnavailable(RuntimeError):
    pass


def ollama_models_dir() -> Path:
    return Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models")


def ollama_model_gguf(model: str, models_dir: Path | None = None) -> Path | None:
    """Resolve an Ollama model name (e.g. 'llama3.1:8b') to its GGUF weights blob on disk."""
    root = models_dir or ollama_models_dir()
    name, _, tag = model.partition(":")
    tag = tag or "latest"
    parts = name.split("/")
    if len(parts) == 1:
        mpath = root / "manifests" / "registry.ollama.ai" / "library" / name / tag
    elif len(parts) == 2:
        mpath = root / "manifests" / "registry.ollama.ai" / parts[0] / parts[1] / tag
    else:
        mpath = root / "manifests" / Path(*parts) / tag
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for layer in manifest.get("layers", []):
        if layer.get("mediaType") == "application/vnd.ollama.image.model":
            blob = root / "blobs" / layer["digest"].replace(":", "-")
            return blob if blob.exists() else None
    return None


class LocalLLMAdapter(Adapter):
    key = "llm"
    name = "Local LLM (Ollama / llama.cpp)"
    purpose = "Drafts one section at a time on your own hardware — nothing leaves the PC."
    strengths = "Follows the Auto / CPU / GPU / Hybrid compute plan; every call is written to the AI-Use Log."
    repo = "https://github.com/ollama/ollama · https://github.com/ggml-org/llama.cpp"
    _llama_proc: subprocess.Popen | None = None
    _llama_key: tuple | None = None

    # ---------------------------------------------------------------- discovery
    def backend(self) -> str | None:
        for name, url, path in (("ollama", OLLAMA, "/api/tags"), ("llamacpp", LLAMACPP, "/health")):
            try:
                if httpx.get(url + path, timeout=0.8).status_code == 200:
                    return name
            except httpx.HTTPError:
                continue
        return None

    def native_available(self) -> bool:
        return self.backend() is not None

    def native_version(self) -> str:
        b = self.backend()
        gpu = " + llama.cpp CUDA" if config.find_binary("llama-server") else ""
        if b == "ollama":
            try:
                return "Ollama " + httpx.get(OLLAMA + "/api/version", timeout=1).json().get("version", "") + gpu
            except httpx.HTTPError:
                return "Ollama" + gpu
        return "llama.cpp server" if b else ""

    def models(self) -> list[str]:
        try:
            return [m["name"] for m in httpx.get(OLLAMA + "/api/tags", timeout=2).json().get("models", [])]
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        try:
            data = httpx.get(LLAMACPP + "/v1/models", timeout=2).json().get("data", [])
            return [m.get("id", "llama.cpp") for m in data] or ["llama.cpp"]
        except (httpx.HTTPError, ValueError):
            return []

    # ---------------------------------------------------------------- managed llama.cpp (GPU path)
    def gpu_server_possible(self, plan: dict, model: str) -> Path | None:
        if plan["effective"] == "cpu" or not config.find_binary("llama-server"):
            return None
        return ollama_model_gguf(model)

    def _llama_log(self) -> Path:
        return config.data_dir() / "logs" / "llama-server.log"

    def stop_llama_server(self) -> None:
        p = LocalLLMAdapter._llama_proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        LocalLLMAdapter._llama_proc, LocalLLMAdapter._llama_key = None, None

    def ensure_llama_server(self, model_path: Path, plan: dict, ctx: int = 8192) -> None:
        """(Re)start llama-server so it matches the plan; raise with the server's own error text on failure."""
        layers = 999 if plan["n_gpu_layers"] == -1 else int(plan["n_gpu_layers"])
        key = (str(model_path), layers, plan["num_thread"], tuple(plan.get("tensor_split") or ()), plan.get("main_gpu"))
        p = LocalLLMAdapter._llama_proc
        if p and p.poll() is None and LocalLLMAdapter._llama_key == key:
            return
        self.stop_llama_server()
        exe = config.find_binary("llama-server")
        cmd = [exe, "-m", str(model_path), "--host", "127.0.0.1", "--port", str(LLAMACPP_PORT),
               "--n-gpu-layers", str(layers), "--threads", str(plan["num_thread"]), "-c", str(ctx), "--jinja"]
        if plan.get("tensor_split"):
            cmd += ["--tensor-split", ",".join(str(x) for x in plan["tensor_split"])]
        if plan.get("main_gpu") is not None and not plan.get("tensor_split"):
            cmd += ["--main-gpu", str(plan["main_gpu"])]
        env = dict(os.environ, **plan_env(plan))
        log = self._llama_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        fh = open(log, "w", encoding="utf-8", errors="replace")
        LocalLLMAdapter._llama_proc = subprocess.Popen(cmd, env=env, creationflags=NO_WINDOW, stdout=fh,
                                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        LocalLLMAdapter._llama_key = key
        t0 = time.time()
        while time.time() - t0 < 300:
            if LocalLLMAdapter._llama_proc.poll() is not None:
                tail = log.read_text(encoding="utf-8", errors="replace")[-800:]
                LocalLLMAdapter._llama_key = None
                raise RuntimeError(f"llama-server exited while loading the model: {tail}")
            try:
                if httpx.get(LLAMACPP + "/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        self.stop_llama_server()
        raise RuntimeError("llama-server did not become ready within 300 s.")

    # ---------------------------------------------------------------- generation
    def generate(self, prompt: str, *, plan: dict, model: str | None = None, system: str = "",
                 temperature: float = 0.6, max_tokens: int = 900, oom_events: list | None = None) -> dict:
        backend = self.backend()
        available = self.models() if backend else []
        model = model or ("llama3.1:8b" if any(m.startswith("llama3.1:8b") for m in available)
                          else (available or ["llama3.1:8b"])[0])
        gguf = self.gpu_server_possible(plan, model)
        if not backend and not gguf:
            raise LLMUnavailable("No local LLM reachable. Start Ollama (ollama serve) and pull a model, "
                                 "or run llama-server on port 8080.")
        engine = "llamacpp-gpu" if gguf else backend

        def call(p: dict) -> dict:
            if engine == "llamacpp-gpu":
                self.ensure_llama_server(gguf, p)
                msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
                r = httpx.post(LLAMACPP + "/v1/chat/completions", json={"messages": msgs, "max_tokens": max_tokens,
                                                                         "temperature": temperature}, timeout=900)
                if r.status_code >= 400:
                    raise RuntimeError(r.text[:300])
                text = r.json()["choices"][0]["message"]["content"].strip()
                return {"text": text, "options": {"n_gpu_layers": p["n_gpu_layers"], "num_thread": p["num_thread"],
                                                  "tensor_split": p.get("tensor_split") or []}}
            if engine == "ollama":
                opts = llm_options(p)
                opts.update(temperature=temperature, num_predict=max_tokens)
                r = httpx.post(OLLAMA + "/api/generate", json={"model": model, "prompt": prompt, "system": system,
                                                               "stream": False, "options": opts}, timeout=900)
                if r.status_code >= 400:
                    raise RuntimeError(r.text[:300])
                return {"text": r.json().get("response", "").strip(), "options": opts}
            full = (system + "\n\n" if system else "") + prompt
            r = httpx.post(LLAMACPP + "/completion", json={"prompt": full, "n_predict": max_tokens,
                                                           "temperature": temperature}, timeout=900)
            if r.status_code >= 400:
                raise RuntimeError(r.text[:300])
            return {"text": r.json().get("content", "").strip(), "options": {"n_gpu_layers": p["n_gpu_layers"]}}

        t0 = time.time()
        out = run_with_oom_fallback(call, plan, oom_events)
        out.update(model=model, backend=engine, seconds=round(time.time() - t0, 2))
        return out


DRAFT_SYSTEM = ("You are an academic writing assistant. Write only the requested section in formal scholarly prose. "
                "Do not invent citations, statistics or results; where evidence is needed insert [CITATION NEEDED] "
                "or use only the citation keys provided, in Pandoc form [@key].")


def draft_prompt(project: dict, section: dict, extra: str, keys: list[str]) -> str:
    return (f"Paper title: {project['title']}\nResearch question: {project.get('question', '')}\n"
            f"Hypothesis: {project.get('hypothesis', '')}\nAuthor's outline:\n{project.get('outline', '')}\n\n"
            f"Write the **{section['name']}** section only (Markdown, no heading).\n"
            f"Available citation keys: {', '.join(keys) if keys else '(none — use [CITATION NEEDED])'}\n"
            f"Author's instructions for this section: {extra or '(none)'}")
