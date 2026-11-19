"""LanguageTool — deep rule-based + n-gram grammar checker (local Java server, port 8081)."""
from __future__ import annotations

import subprocess
import sys
import threading
import time

import httpx

from .. import config
from .base import Adapter, result
from .grammar_rules import deep_check

LT_PORT = 8081
LT_URL = f"http://127.0.0.1:{LT_PORT}/v2"


class LanguageToolAdapter(Adapter):
    key = "languagetool"
    name = "LanguageTool"
    purpose = "Deep contextual grammar, punctuation and academic-syntax checker."
    strengths = "Thousands of rules plus n-gram context; catches subtle agreement and word-choice errors."
    repo = "https://github.com/languagetool-org/languagetool"
    _proc: subprocess.Popen | None = None
    _lock = threading.Lock()

    def jar(self):
        root = config.VENDOR_JAVA
        if root.exists():
            for p in root.rglob("languagetool-server.jar"):
                return p
        return None

    def server_up(self) -> bool:
        try:
            return httpx.get(f"{LT_URL}/languages", timeout=1.0).status_code == 200
        except httpx.HTTPError:
            return False

    def native_available(self) -> bool:
        return self.server_up() or bool(self.jar() and config.find_java())

    def native_version(self) -> str:
        return "server running" if self.server_up() else "jar vendored (starts on demand)"

    def ensure_server(self, wait: float = 25.0) -> bool:
        if self.server_up():
            return True
        jar, java = self.jar(), config.find_java()
        if not (jar and java):
            return False
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                flags = 0x08000000 if sys.platform.startswith("win") else 0
                LanguageToolAdapter._proc = subprocess.Popen(
                    [java, "-Xmx1g", "-cp", str(jar), "org.languagetool.server.HTTPServer",
                     "--port", str(LT_PORT), "--allow-origin", "*"],
                    cwd=str(jar.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        t0 = time.time()
        while time.time() - t0 < wait:
            if self.server_up():
                return True
            time.sleep(0.5)
        return False

    def check(self, text: str, language: str = "en-US") -> dict:
        if self.ensure_server():
            try:
                r = httpx.post(f"{LT_URL}/check", data={"text": text, "language": language,
                                                         "level": "picky"}, timeout=60)
                r.raise_for_status()
                issues = [{"rule": m["rule"]["id"], "start": m["offset"], "end": m["offset"] + m["length"],
                           "message": m["message"], "replacements": [x["value"] for x in m.get("replacements", [])][:3],
                           "severity": "error" if m["rule"].get("issueType") == "misspelling" else "warning",
                           "category": m["rule"].get("category", {}).get("name", "Grammar")}
                          for m in r.json().get("matches", [])]
                return result(self.name, "native", issues=issues)
            except httpx.HTTPError as exc:
                return result(self.name, "builtin", issues=deep_check(text),
                              note=f"LanguageTool server error ({exc}); used built-in deep rules.")
        return result(self.name, "builtin", issues=deep_check(text),
                      note="LanguageTool server/JRE not available; used built-in deep rules.")
