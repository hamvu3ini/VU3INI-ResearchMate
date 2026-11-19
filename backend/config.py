"""Central paths, version and runtime constants for VU3INI ResearchMate."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "VU3INI ResearchMate"
CREATOR = "Sasanka Boruah - VU3INI"
CONTACT_EMAIL = "hamvu3ini@gmail.com"
FOOTER_TEXT = "By Sasanka Boruah -VU3INI"

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
ASSETS_DIR = FRONTEND_DIR / "assets"
VENDOR_DIR = ROOT / "vendor"
VENDOR_BIN = VENDOR_DIR / "bin"
VENDOR_JAVA = VENDOR_DIR / "java"
VENDOR_REPOS = VENDOR_DIR / "repos"
VENDOR_CSL = VENDOR_DIR / "csl"
VALE_STYLES = ROOT / "backend" / "vale_styles"

# User data lives next to the app (portable) unless overridden (tests use a temp dir).
DATA_DIR = Path(os.environ.get("RESEARCHMATE_DATA", ROOT / "data"))
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("RESEARCHMATE_PORT", "8765"))


def version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "1.0.0"
    except OSError:
        return "1.0.0"


def data_dir() -> Path:
    d = Path(os.environ.get("RESEARCHMATE_DATA", DATA_DIR))
    for sub in ("projects", "exports", "uploads", "index", "logs"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


CONDA_TOOLS = VENDOR_BIN / "tools"          # conda-forge win-64 prefix: Ollama, llama.cpp CUDA, Pandoc, Typst, Vale, Harper
_bin_cache: dict[tuple, str | None] = {}


def vendored_tool_dirs() -> list[Path]:
    """Folders of the bundled tools environment, in the order Windows should search them for EXEs/DLLs."""
    if not CONDA_TOOLS.exists():
        return []
    return [d for d in (CONDA_TOOLS / "Library" / "bin", CONDA_TOOLS / "bin", CONDA_TOOLS, CONDA_TOOLS / "Scripts")
            if d.exists()]


def activate_vendored_tools() -> None:
    """Put the bundled tools (and their DLLs: CUDA, cuBLAS, VC runtime…) on PATH for every child process."""
    dirs = [str(d) for d in vendored_tool_dirs()]
    if dirs:
        current = os.environ.get("PATH", "")
        missing = [d for d in dirs if d not in current.split(os.pathsep)]
        if missing:
            os.environ["PATH"] = os.pathsep.join(missing + [current])


def find_binary(*names: str) -> str | None:
    """Locate a CLI tool: vendored copy first (vendor/bin/**), then PATH. Results are cached."""
    key = names
    if key in _bin_cache:
        hit = _bin_cache[key]
        if hit is None or Path(hit).exists():
            return hit
    exe = ".exe" if sys.platform.startswith("win") else ""
    found = None
    for name in names:
        for d in vendored_tool_dirs():
            for cand in (d / (name + exe), d / name):
                if cand.is_file():
                    found = str(cand)
                    break
            if found:
                break
        if not found and VENDOR_BIN.exists():
            for pattern in (name + exe, name):
                found = next((str(c) for c in VENDOR_BIN.rglob(pattern) if c.is_file()), None)
                if found:
                    break
        found = found or shutil.which(name)
        if found:
            break
    _bin_cache[key] = found
    return found


activate_vendored_tools()


def find_java() -> str | None:
    if VENDOR_JAVA.exists():
        for cand in VENDOR_JAVA.rglob("java.exe" if sys.platform.startswith("win") else "java"):
            if cand.is_file() and cand.parent.name == "bin":
                return str(cand)
    return shutil.which("java")
