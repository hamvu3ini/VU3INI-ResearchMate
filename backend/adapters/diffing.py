"""Hunk-level diffs for Step 9: every changed span can be accepted or rejected independently."""
from __future__ import annotations

import difflib
import re

TOKEN = re.compile(r"\s+|[^\s]+")


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text or "")


def _opcodes(original: str, revised: str):
    a, b = tokens(original), tokens(revised)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return a, b, sm.get_opcodes()


def make_hunks(original: str, revised: str, context_tokens: int = 12) -> list[dict]:
    a, b, ops = _opcodes(original, revised)
    offs = [0]
    for t in a:
        offs.append(offs[-1] + len(t))
    hunks = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        hunks.append({"id": len(hunks), "tag": tag, "before": "".join(a[i1:i2]), "after": "".join(b[j1:j2]),
                      "a_start": offs[i1], "a_end": offs[i2],
                      "context_before": "".join(a[max(0, i1 - context_tokens):i1]),
                      "context_after": "".join(a[i2:i2 + context_tokens])})
    return hunks


def apply_decisions(original: str, revised: str, decisions: dict) -> str:
    """decisions: {hunk_id(str|int): 'accept'|'reject'}; undecided hunks are rejected (original kept)."""
    a, b, ops = _opcodes(original, revised)
    dec = {int(k): v for k, v in (decisions or {}).items()}
    out, h = [], 0
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            out.append("".join(a[i1:i2]))
            continue
        out.append("".join(b[j1:j2]) if dec.get(h) == "accept" else "".join(a[i1:i2]))
        h += 1
    return "".join(out)
