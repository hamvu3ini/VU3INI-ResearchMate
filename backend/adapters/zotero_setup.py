"""Automatic Zotero + Better BibTeX provisioning for Step 2 ("Collect Sources & Export .bib").

When Step 2 opens and live sync is not available, ResearchMate:
  1. finds Zotero (Program Files, %LOCALAPPDATA%, PATH, or a portable copy in vendor\\bin\\zotero) — if missing,
     runs the bundled installer (vendor\\installers\\Zotero-*setup*.exe) or downloads it from zotero.org, silently (-ms)
  2. starts Zotero and waits for its local connector (127.0.0.1:23119)
  3. if Better BibTeX is missing: closes Zotero gracefully (like clicking X — never a forced kill), copies the
     BBT .xpi (bundled or the latest GitHub release) into the default Zotero profile, enables side-loaded add-ons
     and Zotero's local API in user.js, and starts Zotero again
  4. waits until Better BibTeX reports "ready" (its first start indexes the library, which can take a while)
Runs as a background job; the browser polls /api/zotero/setup/status for a live progress card.
"""
from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

import httpx

from .. import config
from ..netstatus import is_online

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

CONNECTOR = "http://127.0.0.1:23119"
BBT_ID = "better-bibtex@iris-advies.com"
ZOTERO_INSTALLER_URL = "https://www.zotero.org/download/client/dl?channel=release&platform=win-x64"
BBT_RELEASES_API = "https://api.github.com/repos/retorquere/zotero-better-bibtex/releases/latest"
USER_PREFS = {
    "extensions.autoDisableScopes": "0",                     # don't disable side-loaded add-ons (BBT)
    "extensions.enabledScopes": "15",
    "extensions.zotero.httpServer.localAPI.enabled": "true",  # lets ResearchMate list your collections
}
NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0
DETACHED = 0x00000008 if sys.platform.startswith("win") else 0
EXE = "zotero.exe" if sys.platform.startswith("win") else "zotero"


def zotero_profile_root() -> Path:
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "Zotero" / "Zotero"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Zotero"
    return Path.home() / ".zotero" / "zotero"


def default_profile(root: Path | None = None) -> Path | None:
    """Read profiles.ini and return the default profile folder (or the only one)."""
    root = root or zotero_profile_root()
    ini = root / "profiles.ini"
    if not ini.exists():
        return None
    cp = configparser.ConfigParser()
    cp.read(ini, encoding="utf-8")
    profiles = [cp[s] for s in cp.sections() if s.lower().startswith("profile") and "Path" in cp[s]]
    if not profiles:
        return None
    chosen = next((p for p in profiles if p.get("Default") == "1"), profiles[0])
    path = Path(chosen["Path"])
    return (root / path) if chosen.get("IsRelative", "1") == "1" else path


def write_user_prefs(profile: Path, prefs: dict[str, str] = USER_PREFS) -> list[str]:
    """Add/replace prefs in <profile>/user.js (read by Zotero at every start). Returns the keys that changed."""
    uj = profile / "user.js"
    lines = uj.read_text(encoding="utf-8").splitlines() if uj.exists() else []
    changed = []
    for key, val in prefs.items():
        line = f'user_pref("{key}", {val});'
        idx = next((i for i, l in enumerate(lines) if f'"{key}"' in l), None)
        if idx is None:
            lines.append(line)
            changed.append(key)
        elif lines[idx].strip() != line:
            lines[idx] = line
            changed.append(key)
    if changed:
        profile.mkdir(parents=True, exist_ok=True)
        uj.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


class ZoteroSetup:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.job: dict = {"stage": "idle", "progress": 0.0, "message": "", "error": None, "done": False, "log": []}

    # ---------------------------------------------------------------- probes (patched in tests)
    def find_exe(self) -> str | None:
        cands = []
        if sys.platform.startswith("win"):
            for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
                base = os.environ.get(var)
                if base:
                    cands += [Path(base) / "Zotero" / EXE, Path(base) / "Programs" / "Zotero" / EXE]
        cands += [config.VENDOR_BIN / "zotero" / EXE]
        for c in cands:
            if c.exists():
                return str(c)
        return shutil.which("zotero")

    def is_running(self) -> bool:
        try:
            return httpx.get(CONNECTOR + "/connector/ping", timeout=1.5).status_code == 200
        except httpx.HTTPError:
            return False

    def process_alive(self) -> bool:
        if not psutil:
            return self.is_running()
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() in ("zotero.exe", "zotero"):
                return True
        return False

    def bbt_ready(self) -> bool:
        try:
            r = httpx.get(CONNECTOR + "/better-bibtex/cayw", params={"probe": "true"}, timeout=2)
            return r.status_code == 200 and "ready" in r.text
        except httpx.HTTPError:
            return False

    def bbt_responding(self) -> bool:
        """BBT loaded but maybe still initialising (probe answers something other than 'ready')."""
        try:
            return httpx.get(CONNECTOR + "/better-bibtex/cayw", params={"probe": "true"}, timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    def profile(self) -> Path | None:
        return default_profile()

    def bbt_installed(self) -> bool:
        prof = self.profile()
        if not prof:
            return False
        ext = prof / "extensions"
        return (ext / f"{BBT_ID}.xpi").exists() or (ext / BBT_ID).exists() or \
            BBT_ID in ((prof / "extensions.json").read_text(encoding="utf-8", errors="ignore")
                       if (prof / "extensions.json").exists() else "")

    def vendored_installer(self) -> Path | None:
        d = config.VENDOR_DIR / "installers"
        return next(iter(sorted(d.glob("Zotero*setup*.exe"))), None) if d.exists() else None

    def vendored_xpi(self) -> Path | None:
        d = config.VENDOR_DIR / "installers"
        return next(iter(sorted(d.glob("zotero-better-bibtex*.xpi"))), None) if d.exists() else None

    def status(self) -> dict:
        exe = self.find_exe()
        running = self.is_running()
        ready = running and self.bbt_ready()
        return {"installed": bool(exe), "binary": exe, "running": running, "bbt_installed": self.bbt_installed(),
                "bbt_ready": ready, "ready": ready, "profile": str(self.profile() or ""),
                "vendored_installer": bool(self.vendored_installer()), "vendored_xpi": bool(self.vendored_xpi()),
                "platform": sys.platform, "job": dict(self.job, log=self.job["log"][-12:])}

    # ---------------------------------------------------------------- actions (patched in tests)
    def install_zotero(self, progress: Callable[[float, str], None]) -> str:
        if not sys.platform.startswith("win"):
            raise RuntimeError("Automatic Zotero install is only supported on Windows. Install it from zotero.org.")
        installer = self.vendored_installer()
        if not installer:
            if not is_online():
                raise RuntimeError("Zotero is not installed, no installer is bundled in vendor\\installers and the "
                                   "PC is offline. Connect to the internet, or import a .bib file instead.")
            installer = config.data_dir() / "downloads" / "Zotero_setup.exe"
            installer.parent.mkdir(parents=True, exist_ok=True)
            self._download(ZOTERO_INSTALLER_URL, installer, progress, "Zotero installer")
        progress(-1, "Installing Zotero silently — approve the Windows (UAC) prompt if it appears…")
        proc = subprocess.run([str(installer), "-ms"], timeout=900)
        if proc.returncode != 0:
            raise RuntimeError(f"Zotero installer exited with code {proc.returncode} (was the UAC prompt declined?).")
        for _ in range(30):
            exe = self.find_exe()
            if exe:
                return exe
            time.sleep(1)
        raise RuntimeError("Zotero installer finished but zotero.exe was not found.")

    def get_xpi(self, progress: Callable[[float, str], None]) -> Path:
        xpi = self.vendored_xpi()
        if xpi:
            return xpi
        if not is_online():
            raise RuntimeError("Better BibTeX is not installed, no .xpi is bundled in vendor\\installers and the PC "
                               "is offline. Connect to the internet, or import a .bib file instead.")
        req = urllib.request.Request(BBT_RELEASES_API, headers={"User-Agent": "VU3INI-ResearchMate/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            rel = json.loads(r.read())
        asset = next(a for a in rel["assets"] if re.match(r"zotero-better-bibtex-.*\.xpi$", a["name"]))
        dest = config.data_dir() / "downloads" / asset["name"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._download(asset["browser_download_url"], dest, progress, "Better BibTeX")
        return dest

    def start_zotero(self, exe: str) -> None:
        subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         creationflags=DETACHED, close_fds=True)

    def close_zotero_gracefully(self, timeout: float = 45) -> bool:
        """Ask Zotero to close like clicking X (WM_CLOSE) so its database is saved. Never force-kills."""
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/IM", "zotero.exe"], capture_output=True, creationflags=NO_WINDOW)
        elif psutil:
            for p in psutil.process_iter(["name"]):
                if (p.info.get("name") or "").lower() == "zotero":
                    p.terminate()
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not self.process_alive():
                return True
            time.sleep(1)
        return False

    def install_xpi(self, xpi: Path, profile: Path) -> None:
        ext = profile / "extensions"
        ext.mkdir(parents=True, exist_ok=True)
        shutil.copy2(xpi, ext / f"{BBT_ID}.xpi")
        write_user_prefs(profile)

    @staticmethod
    def _download(url: str, dest: Path, progress: Callable[[float, str], None], label: str) -> None:
        tmp = dest.with_suffix(dest.suffix + ".part")
        req = urllib.request.Request(url, headers={"User-Agent": "VU3INI-ResearchMate/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as fh:
            total = int(r.headers.get("content-length") or 0)
            done = 0
            while chunk := r.read(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                progress(done / total if total else -1, f"Downloading {label} — {done / 1e6:.0f} MB")
        tmp.replace(dest)

    # ---------------------------------------------------------------- job
    def _set(self, stage=None, progress=None, message=None):
        if stage:
            self.job["stage"] = stage
        if progress is not None:
            self.job["progress"] = round(progress, 4)
        if message:
            self.job["message"] = message
            if not self.job["log"] or self.job["log"][-1] != message:
                self.job["log"].append(message)

    def _wait(self, cond: Callable[[], bool], seconds: float, msg: str, stage: str) -> bool:
        t0 = time.time()
        while time.time() - t0 < seconds:
            if cond():
                return True
            self._set(stage, -1, f"{msg} ({int(time.time() - t0)} s)")
            time.sleep(1)
        return cond()

    def run(self) -> dict:
        self.job.update(stage="checking", progress=0.0, message="Checking for Zotero…", error=None, done=False,
                        log=["Checking for Zotero…"])
        try:
            if self.is_running() and self.bbt_ready():
                self._set("ready", 1.0, "Zotero is running and Better BibTeX is ready.")
                return self.status()
            exe = self.find_exe()
            if not exe:
                self._set("installing", 0.0, "Zotero not found — installing it now.")
                exe = self.install_zotero(lambda p, m: self._set(None, p, m))
                self._set("installing", 1.0, f"Zotero installed at {exe}")
            if not self.is_running():
                self._set("starting", -1, "Starting Zotero…")
                self.start_zotero(exe)
                if not self._wait(self.is_running, 120, "Waiting for Zotero to start", "starting"):
                    raise RuntimeError("Zotero did not start (its connector on 127.0.0.1:23119 never answered).")
            self._set("starting", 1.0, "Zotero is running.")
            if not self.bbt_responding():
                if self.bbt_installed():
                    self._set("bbt", -1, "Better BibTeX is installed — waiting for it to load…")
                else:
                    xpi = self.get_xpi(lambda p, m: self._set("bbt", p, m))
                    profile = self.profile()
                    if not profile:
                        raise RuntimeError("Could not find the Zotero profile (profiles.ini).")
                    self._set("bbt", -1, "Closing Zotero normally to install Better BibTeX…")
                    if not self.close_zotero_gracefully():
                        raise RuntimeError("Zotero did not close. Close Zotero yourself (finish any open edits), "
                                           "then press Retry — ResearchMate will install Better BibTeX.")
                    self.install_xpi(xpi, profile)
                    self._set("bbt", -1, f"Better BibTeX installed into {profile.name}; restarting Zotero…")
                    self.start_zotero(exe)
                    if not self._wait(self.is_running, 120, "Waiting for Zotero to restart", "bbt"):
                        raise RuntimeError("Zotero did not restart after installing Better BibTeX.")
            elif not self.profile() or write_user_prefs(self.profile()) == []:
                pass  # BBT already there; prefs (local API) already set or profile unknown
            if not self._wait(self.bbt_ready, 600, "Better BibTeX is indexing your library (first start only)",
                              "indexing"):
                raise RuntimeError("Better BibTeX did not report ready within 10 minutes. If Zotero shows an "
                                   "add-on prompt, allow Better BibTeX, then press Retry.")
            self._set("ready", 1.0, "Zotero + Better BibTeX are running — live sync is ready.")
        except Exception as exc:  # noqa: BLE001
            self.job.update(failed_stage=self.job["stage"], stage="error", error=str(exc), message=str(exc))
            self.job["log"].append("ERROR: " + str(exc))
        finally:
            self.job["done"] = True
        return self.status()

    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self.job.update(stage="queued", done=False, error=None, message="Starting Zotero setup…")
            self._thread = threading.Thread(target=self.run, daemon=True, name="zotero-setup")
            self._thread.start()
        return self.status()
