"""XtraGPT (clarity / academic flow) and PaperDebugger (logic & methodology critique) revision engines.

Native path: the local LLM (an XtraGPT GGUF pulled into Ollama when available, otherwise the active model)
prompted in each tool's style. Built-in path: deterministic rule-based revisions so Step 8 still works offline
with no model at all. Both return revised text; diffing into accept/reject hunks happens in diffing.py."""
from __future__ import annotations

import json
import re

from .base import Adapter, split_sentences
from .llm import LLMUnavailable, LocalLLMAdapter
from .vale import WORDY

CONCISE = dict(WORDY, **{"it should be noted that ": "", "it is interesting to note that ": "",
                         "in the case of": "for", "a number of": "several", "in terms of": "in",
                         "has been shown to be": "is", "serves to": "", "the reason why is that": "because",
                         "whether or not": "whether", "each and every": "every", "first and foremost": "first",
                         "very ": "", "really ": "", "basically ": "", "actually ": ""})
OVERCLAIM = {"proves": "suggests", "prove": "suggest", "proven": "supported", "clearly shows": "indicates",
             "clearly demonstrates": "indicates", "always": "consistently", "never": "rarely",
             "undoubtedly": "likely", "obviously": "", "certainly": "likely", "definitively": ""}
TRANSITIONS = ("however", "therefore", "moreover", "in contrast", "consequently", "similarly", "thus", "first",
               "second", "finally", "in addition", "as a result", "by contrast", "specifically", "accordingly")


def _apply_table(text: str, table: dict) -> tuple[str, list[str]]:
    notes = []
    for k, v in sorted(table.items(), key=lambda kv: -len(kv[0])):
        pat = re.compile(r"(?<!\w)" + re.escape(k.strip()) + (r"\s?" if k.endswith(" ") else r"(?!\w)"), re.I)
        if pat.search(text):
            def rep(m, v=v):
                s = v
                if m.group(0)[:1].isupper() and s:
                    s = s[0].upper() + s[1:]
                return s + (" " if k.endswith(" ") and s else "")
            text = pat.sub(rep, text)
            notes.append(f"“{k.strip()}” → “{v.strip() or '(removed)'}”")
    text = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    text = re.sub(r"  +", " ", text)
    return text, notes


class XtraGPTAdapter(Adapter):
    key = "xtragpt"
    name = "XtraGPT"
    purpose = "Sentence-level academic revision: clarity, concision and scholarly flow."
    strengths = "Context-aware, controllable edits that keep your meaning; ideal polish pass."
    repo = "https://github.com/nuojohnchen/XtraGPT"

    def __init__(self, llm: LocalLLMAdapter):
        self.llm = llm

    def native_available(self) -> bool:
        return self.llm.native_available()

    def native_version(self) -> str:
        xm = [m for m in self.llm.models() if "xtra" in m.lower()]
        return f"model {xm[0]}" if xm else "via active local LLM"

    def revise(self, text: str, paper_context: str, plan: dict, instruction: str = "") -> dict:
        if self.native_available():
            models = self.llm.models()
            model = next((m for m in models if "xtra" in m.lower()), None)
            prompt = (f"<PAPER_CONTENT>\n{paper_context[:6000]}\n</PAPER_CONTENT>\n<SELECTED_CONTENT>\n{text}\n"
                      f"</SELECTED_CONTENT>\n<QUESTION>\n{instruction or 'Improve clarity, concision and academic flow. Keep every claim, number and citation key unchanged.'}"
                      f"\n</QUESTION>\nReturn ONLY the revised selected content.")
            try:
                out = self.llm.generate(prompt, plan=plan, model=model, temperature=0.3,
                                        system="Act as an expert model for improving academic papers.")
                return {"engine": "native", "model": out["model"], "revised": out["text"] or text,
                        "notes": ["Revised by " + out["model"]], "prompt": prompt}
            except (LLMUnavailable, RuntimeError):
                pass
        revised, notes = _apply_table(text, CONCISE)
        return {"engine": "builtin", "model": "builtin-rules", "revised": revised,
                "notes": notes or ["No concision edits found."], "prompt": "builtin concision rules"}


class PaperDebuggerAdapter(Adapter):
    key = "paperdebugger"
    name = "PaperDebugger"
    purpose = "Structural & logical critique: argument gaps, weak transitions, methodological weaknesses."
    strengths = "Reviewer-style diagnosis — tells you what is missing, not just how to phrase it."
    repo = "(PaperDebugger, arXiv:2512.02589 — set repo URL in scripts/vendor_manifest.json)"

    def __init__(self, llm: LocalLLMAdapter):
        self.llm = llm

    def native_available(self) -> bool:
        return self.llm.native_available()

    def native_version(self) -> str:
        return "via active local LLM"

    def revise(self, text: str, section_name: str, plan: dict, instruction: str = "") -> dict:
        critique = self.critique(text, section_name)
        if self.native_available():
            prompt = ("You are a rigorous peer reviewer. Critique the section below for argument gaps, unsupported "
                      "claims, weak transitions and methodological weaknesses, then rewrite it to fix them without "
                      "inventing data or citations.\nReturn JSON: {\"issues\": [\"...\"], \"revised\": \"...\"}\n\n"
                      f"Section: {section_name}\n{instruction}\n---\n{text}")
            try:
                out = self.llm.generate(prompt, plan=plan, temperature=0.2)
                m = re.search(r"\{.*\}", out["text"], re.S)
                data = json.loads(m.group(0)) if m else {}
                return {"engine": "native", "model": out["model"], "revised": data.get("revised") or text,
                        "notes": data.get("issues") or critique, "prompt": prompt}
            except (LLMUnavailable, RuntimeError, json.JSONDecodeError):
                pass
        revised, notes = _apply_table(text, OVERCLAIM)
        return {"engine": "builtin", "model": "builtin-critic", "revised": revised,
                "notes": critique + notes, "prompt": "builtin critique rules"}

    @staticmethod
    def critique(text: str, section_name: str) -> list[str]:
        issues = []
        sents = split_sentences(text)
        paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
        for i, p in enumerate(paras[1:], start=2):
            first = p.strip().split(" ", 3)[:3]
            if not any(" ".join(first).lower().startswith(t) for t in TRANSITIONS):
                issues.append(f"Paragraph {i} opens without a transition — link it to the previous point.")
        for s in sents:
            if re.search(r"\b(show|shows|showed|demonstrate[sd]?|prove[sn]?|establish(?:es|ed)?|found)\b", s["text"], re.I) \
                    and not re.search(r"\[@|\\cite|\(\w+,? \d{4}\)|\[\d+\]|Table|Figure|Fig\.", s["text"]):
                issues.append(f"Claim without evidence or citation: “{s['text'][:90]}…”")
        for w in OVERCLAIM:
            if re.search(r"\b" + re.escape(w) + r"\b", text, re.I):
                issues.append(f"Overclaiming language: “{w}”.")
        if section_name.lower().startswith("method"):
            for need, label in ((r"\b(n\s*=|participants|samples|dataset)\b", "sample/dataset size"),
                                (r"\b(test|regression|anova|t-test|chi|bootstrap|p\s*[<=])\b", "statistical test"),
                                (r"\b(version|v\d|software|library|python|R\b)", "software/tool versions")):
                if not re.search(need, text, re.I):
                    issues.append(f"Methods do not report {label} — needed for reproducibility.")
        if section_name.lower().startswith("result") and not re.search(r"\d", text):
            issues.append("Results contain no quantitative values.")
        return issues or ["No structural issues detected."]
