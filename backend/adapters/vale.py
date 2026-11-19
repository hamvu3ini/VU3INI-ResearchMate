"""Vale — programmable prose linter. ResearchMate ships its own Vale styles (AI clichés, hedging,
wordiness, plus IEEE / APA / Nature house-style rules). The same rule data powers the built-in fallback."""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from .. import config
from .base import Adapter, cli_version, result, run_cli

AI_CLICHES = [
    "delve into", "delves into", "delving into", "in the realm of", "tapestry", "a testament to", "it is worth noting",
    "it's worth noting", "it is important to note", "plays a crucial role", "plays a pivotal role", "pivotal",
    "navigate the complexities", "ever-evolving", "ever-changing landscape", "in today's fast-paced", "underscores",
    "showcasing", "seamlessly", "a myriad of", "multifaceted", "intricate interplay", "shed light on", "sheds light on",
    "paving the way", "paves the way", "harness the power", "unlock the potential", "groundbreaking", "game-changer",
    "cutting-edge", "robust framework", "holistic approach", "nuanced understanding", "in conclusion,",
    "to summarize,", "furthermore,", "moreover,", "additionally,", "notably,", "embark on", "realm", "landscape of",
]
HEDGES = ["arguably", "it could be argued", "somewhat", "relatively", "fairly", "quite", "perhaps", "possibly",
          "it seems that", "to some extent", "in some sense"]
WORDY = {"in order to": "to", "due to the fact that": "because", "a large number of": "many",
         "at this point in time": "now", "in the event that": "if", "with regard to": "about",
         "for the purpose of": "for", "has the ability to": "can", "is able to": "can", "in spite of the fact that": "although",
         "a majority of": "most", "prior to": "before", "subsequent to": "after", "it is clear that": "",
         "the fact that": "that", "in light of the fact that": "because", "on a daily basis": "daily",
         "make use of": "use", "utilize": "use", "utilization": "use"}
HOUSE = {
    "IEEE": {"swap": {"e.g. ": "e.g., ", "i.e. ": "i.e., ", "Fig ": "Fig. ", "et. al": "et al."},
             "existence": [r"\bwe (?:believe|feel)\b"], "message": "IEEE style"},
    "APA": {"swap": {"e.g. ": "e.g., ", "i.e. ": "i.e., ", " & ": " and ", "percent": "%", "utilise": "use"},
            "existence": [r"\b(?:he/she|s/he)\b", r"\bsubjects\b"], "message": "APA 7 style"},
    "Nature": {"swap": {"e.g. ": "for example, ", "i.e. ": "that is, ", "utilise": "use", "novel": "new"},
               "existence": [r"\bfor the first time\b", r"\bnovel\b"], "message": "Nature style"},
}


def _w(path: Path, txt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(txt, encoding="utf-8")


def ensure_vale_styles() -> Path:
    """Write real Vale YAML rules so vale.exe uses exactly the same checks as the fallback."""
    root = config.VALE_STYLES
    rm = root / "styles" / "ResearchMate"
    q = lambda xs: "\n".join(f"  - '{x.replace(chr(39), chr(39) * 2)}'" for x in xs)  # noqa: E731
    _w(rm / "AICliches.yml", "extends: existence\nmessage: \"AI-cliché phrasing: '%s' — rewrite in your own words.\"\n"
       "level: warning\nignorecase: true\ntokens:\n" + q(AI_CLICHES) + "\n")
    _w(rm / "Hedging.yml", "extends: existence\nmessage: \"Hedge word '%s' weakens the claim.\"\nlevel: suggestion\n"
       "ignorecase: true\ntokens:\n" + q(HEDGES) + "\n")
    _w(rm / "Wordiness.yml", "extends: substitution\nmessage: \"Prefer '%s' over '%s'.\"\nlevel: suggestion\n"
       "ignorecase: true\nswap:\n" + "\n".join(f"  '{k}': '{v or '(delete)'}'" for k, v in WORDY.items()) + "\n")
    for style, spec in HOUSE.items():
        _w(root / "styles" / style / "Swap.yml", f"extends: substitution\nmessage: \"{spec['message']}: use '%s' "
           f"instead of '%s'.\"\nlevel: warning\nnonword: true\nswap:\n" +
           "\n".join(f"  '{re.escape(k)}': '{v}'" for k, v in spec["swap"].items()) + "\n")
        _w(root / "styles" / style / "Avoid.yml", f"extends: existence\nmessage: \"{spec['message']}: avoid '%s'.\"\n"
           f"level: warning\nignorecase: true\nraw:\n" + "\n".join(f"  - '{p}'" for p in spec["existence"]) + "\n")
    for style in HOUSE:
        _w(root / f".vale-{style}.ini", f"StylesPath = styles\nMinAlertLevel = suggestion\n\n[*.md]\n"
           f"BasedOnStyles = ResearchMate, {style}\n")
    return root


class ValeAdapter(Adapter):
    key = "vale"
    name = "Vale"
    purpose = "Programmable prose linter enforcing IEEE / APA / Nature house styles and AI-cliché rules."
    strengths = "Rule packs are plain YAML — editable, versionable and journal-specific."
    repo = "https://github.com/errata-ai/vale"

    def binary(self):
        return config.find_binary("vale")

    def native_available(self) -> bool:
        return bool(self.binary())

    def native_version(self) -> str:
        return cli_version(self.binary(), "--version")

    def lint(self, text: str, style: str = "APA") -> dict:
        style = style if style in HOUSE else "APA"
        root = ensure_vale_styles()
        exe = self.binary()
        if exe:
            try:
                with tempfile.TemporaryDirectory() as td:
                    p = Path(td) / "section.md"
                    p.write_text(text, encoding="utf-8")
                    proc = run_cli([exe, "--output=JSON", f"--config={root / f'.vale-{style}.ini'}", str(p)], timeout=60)
                    data = json.loads(proc.stdout or "{}")
                alerts = next(iter(data.values()), []) if data else []
                line_starts = [0] + [m.end() for m in re.finditer("\n", text)]
                issues = []
                for a in alerts:
                    ls = line_starts[a["Line"] - 1] if a["Line"] - 1 < len(line_starts) else 0
                    issues.append({"rule": a["Check"], "start": ls + a["Span"][0] - 1, "end": ls + a["Span"][1],
                                   "message": a["Message"], "severity": a.get("Severity", "warning"),
                                   "replacements": [], "category": a["Check"].split(".")[0]})
                return result(self.name, "native", issues=issues, style=style)
            except Exception as exc:  # noqa: BLE001
                return result(self.name, "builtin", issues=self.builtin(text, style), style=style,
                              note=f"vale failed ({exc}); used built-in rules.")
        return result(self.name, "builtin", issues=self.builtin(text, style), style=style,
                      note="vale.exe not found in vendor/bin; used built-in copy of the same rules.")

    @staticmethod
    def builtin(text: str, style: str) -> list[dict]:
        issues = []

        def add(rule, m, msg, sev, repl=None):
            issues.append({"rule": rule, "start": m.start(), "end": m.end(), "message": msg, "severity": sev,
                           "replacements": [repl] if repl is not None else [], "category": rule.split(".")[0]})
        for phrase in AI_CLICHES:
            for m in re.finditer(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text, re.I):
                add("ResearchMate.AICliches", m, f"AI-cliché phrasing: '{m.group(0)}' — rewrite in your own words.",
                    "warning")
        for h in HEDGES:
            for m in re.finditer(r"\b" + re.escape(h) + r"\b", text, re.I):
                add("ResearchMate.Hedging", m, f"Hedge word '{m.group(0)}' weakens the claim.", "suggestion")
        for k, v in WORDY.items():
            for m in re.finditer(r"\b" + re.escape(k) + r"\b", text, re.I):
                add("ResearchMate.Wordiness", m, f"Prefer '{v or '(delete)'}' over '{m.group(0)}'.", "suggestion", v)
        spec = HOUSE[style]
        for k, v in spec["swap"].items():
            for m in re.finditer(re.escape(k), text):
                add(f"{style}.Swap", m, f"{spec['message']}: use '{v.strip()}' instead of '{k.strip()}'.", "warning", v)
        for pat in spec["existence"]:
            for m in re.finditer(pat, text, re.I):
                add(f"{style}.Avoid", m, f"{spec['message']}: avoid '{m.group(0)}'.", "warning")
        return sorted(issues, key=lambda i: i["start"])
