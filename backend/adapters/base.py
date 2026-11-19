"""Shared adapter plumbing: CLI runner, status reporting and text utilities."""
from __future__ import annotations

import math
import re
import subprocess
import sys
from collections import Counter
from typing import Any

from .. import config


class Adapter:
    """Every tool adapter reports whether the real (native) tool is present and which engine ran.

    engine = "native"  → the vendored/installed tool (binary, JAR, service or Python package) did the work
    engine = "builtin" → ResearchMate's own offline implementation of the same check was used
    """
    key = "base"
    name = "Base"
    purpose = ""
    strengths = ""
    repo = ""

    def native_available(self) -> bool:
        return False

    def native_version(self) -> str:
        return ""

    def status(self) -> dict:
        native = False
        ver = ""
        try:
            native = self.native_available()
            ver = self.native_version() if native else ""
        except Exception:  # noqa: BLE001
            native = False
        return {"key": self.key, "name": self.name, "purpose": self.purpose, "strengths": self.strengths,
                "repo": self.repo, "native_available": native, "native_version": ver,
                "engine": "native" if native else "builtin"}


def run_cli(cmd: list[str], *, input_text: str | None = None, timeout: float = 120, env: dict | None = None,
            cwd: str | None = None) -> subprocess.CompletedProcess:
    import os
    flags = 0x08000000 if sys.platform.startswith("win") else 0
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout,
                          creationflags=flags, env=full_env, cwd=cwd, encoding="utf-8", errors="replace")


def cli_version(binary: str | None, *args: str) -> str:
    if not binary:
        return ""
    try:
        out = run_cli([binary, *(args or ("--version",))], timeout=10)
        return (out.stdout or out.stderr).strip().splitlines()[0][:80]
    except Exception:  # noqa: BLE001
        return "present"


# ---------------------------------------------------------------- text utilities
_ABBREV = r"(?<!\be\.g)(?<!\bi\.e)(?<!\bet al)(?<!\bFig)(?<!\bEq)(?<!\bvs)(?<!\bDr)(?<!\bMr)(?<!\bMs)(?<!\bNo)"
_SENT_RE = re.compile(_ABBREV + r"(?<=[.!?])[\"')\]]*\s+(?=[A-Z0-9\"'(\[])")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
STOP = set("""a an the and or but if then of to in on for with by from as at is are was were be been being this that
these those it its we our they their there here which who whom whose what when where why how than such into onto over
under between among within without about across after before during also not no can could may might should would will
shall do does did has have had i you he she them his her not only both either neither each per via""".split())


def split_sentences(text: str) -> list[dict]:
    """Return sentences with character offsets so the UI can highlight them in place."""
    out = []
    for para_match in re.finditer(r"[^\n]+", text):
        para = para_match.group(0)
        base = para_match.start()
        if para.lstrip().startswith("#"):
            continue
        start = 0
        for m in _SENT_RE.finditer(para):
            seg = para[start:m.start() + 1] if para[m.start()] in ".!?" else para[start:m.start()]
            seg_end = start + len(seg)
            if seg.strip():
                lead = len(seg) - len(seg.lstrip())
                out.append({"text": seg.strip(), "start": base + start + lead, "end": base + seg_end})
            start = m.end()
        tail = para[start:]
        if tail.strip():
            lead = len(tail) - len(tail.lstrip())
            out.append({"text": tail.strip(), "start": base + start + lead, "end": base + len(para.rstrip())})
    return out


def words(text: str) -> list[str]:
    return [w.lower() for w in WORD_RE.findall(text)]


def content_words(text: str) -> list[str]:
    return [w for w in words(text) if w not in STOP and len(w) > 2]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.docs = [content_words(d) for d in docs]
        self.N = len(self.docs)
        self.avgdl = (sum(len(d) for d in self.docs) / self.N) if self.N else 0
        self.df: Counter = Counter()
        for d in self.docs:
            self.df.update(set(d))
        self.tf = [Counter(d) for d in self.docs]
        self.k1, self.b = k1, b

    def idf(self, term: str) -> float:
        n = self.df.get(term, 0)
        return math.log(1 + (self.N - n + 0.5) / (n + 0.5))

    def scores(self, query: str) -> list[float]:
        q = content_words(query)
        res = []
        for i, tf in enumerate(self.tf):
            dl = len(self.docs[i]) or 1
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    s += self.idf(t) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
            res.append(s)
        return res


def shingles(text: str, n: int = 5) -> set[tuple]:
    w = words(text)
    return {tuple(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def strip_markup(text: str) -> str:
    text = re.sub(r"\[@[^\]]+\]", "", text)
    text = re.sub(r"\\cite[pt]?\{[^}]*\}", "", text)
    text = re.sub(r"[*_`#>]", "", text)
    return text


def result(tool: str, engine: str, **payload: Any) -> dict:
    return {"tool": tool, "engine": engine, "ok": True, **payload}


def data_root():
    return config.data_dir()
