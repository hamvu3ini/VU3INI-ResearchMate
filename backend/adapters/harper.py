"""Harper — Automattic's Rust grammar checker (harper-cli). Instant, fully local."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .. import config
from .base import Adapter, cli_version, result, run_cli
from .grammar_rules import fast_check


class HarperAdapter(Adapter):
    key = "harper"
    name = "Harper"
    purpose = "Ultra-fast local grammar & spelling checker written in Rust."
    strengths = "Millisecond latency, private, ideal for real-time linting while you type."
    repo = "https://github.com/Automattic/harper"

    def binary(self):
        return config.find_binary("harper-cli", "harper")

    def native_available(self) -> bool:
        return bool(self.binary())

    def native_version(self) -> str:
        return cli_version(self.binary(), "--version")

    def check(self, text: str) -> dict:
        exe = self.binary()
        if exe:
            try:
                return result(self.name, "native", issues=self._native(exe, text))
            except Exception as exc:  # noqa: BLE001 — fall back, but tell the user why
                return result(self.name, "builtin", issues=fast_check(text),
                              note=f"harper-cli failed ({exc}); used built-in fast rules.")
        return result(self.name, "builtin", issues=fast_check(text),
                      note="harper-cli not found in vendor/bin; used built-in fast rules.")

    def _native(self, exe: str, text: str) -> list[dict]:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "section.md"
            p.write_text(text, encoding="utf-8")
            proc = run_cli([exe, "lint", "--format", "json", str(p)], timeout=60)
            raw = proc.stdout.strip()
            data = json.loads(raw) if raw.startswith(("[", "{")) else None
            if data is None:
                raise RuntimeError((proc.stderr or proc.stdout)[:160] or "no JSON output")
            lints = data if isinstance(data, list) else data.get("lints") or next(iter(data.values()), [])
            issues = []
            for l in lints:
                span = l.get("span") or {}
                start = span.get("start", l.get("start", 0))
                end = span.get("end", l.get("end", start))
                sugg = [s.get("ReplaceWith", s) if isinstance(s, dict) else s for s in l.get("suggestions", [])]
                sugg = ["".join(s) if isinstance(s, list) else str(s) for s in sugg]
                issues.append({"rule": l.get("lint_kind", l.get("kind", "Harper")), "start": start, "end": end,
                               "message": l.get("message", ""), "replacements": sugg[:3],
                               "severity": "warning", "category": l.get("lint_kind", "Grammar")})
            return issues
