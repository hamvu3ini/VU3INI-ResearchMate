"""Tolerant BibTeX parser/serializer and in-text citation key extraction (Markdown + LaTeX)."""
from __future__ import annotations

import re

ENTRY_START = re.compile(r"@(\w+)\s*([{(])", re.M)
CITE_MD = re.compile(r"(?<![\w@])@([A-Za-z0-9_](?:[\w:.#$%&\-+?<>~/]*\w)?)")
CITE_TEX = re.compile(r"\\(?:cite|citep|citet|parencite|textcite|autocite|footcite)\*?(?:\[[^\]]*\]){0,2}\{([^}]*)\}")


def parse_bib(text: str) -> dict:
    """Return {'entries': [...], 'errors': [...]} — never raises on malformed input."""
    entries, errors = [], []
    pos = 0
    while True:
        m = ENTRY_START.search(text, pos)
        if not m:
            break
        etype = m.group(1).lower()
        open_ch = m.group(2)
        close_ch = "}" if open_ch == "{" else ")"
        i, depth = m.end(), 1
        nxt_entry = ENTRY_START.search(text, m.end())
        limit = len(text)
        while i < limit and depth:
            c = text[i]
            if c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
            i += 1
            # an '@type{' at line start while still open means this entry never closed
            if depth and nxt_entry and i >= nxt_entry.start() and text[nxt_entry.start() - 1:nxt_entry.start()] in ("\n", ""):
                break
        body = text[m.end(): i - 1] if depth == 0 else text[m.end():]
        line = text.count("\n", 0, m.start()) + 1
        if depth != 0:
            errors.append({"line": line, "message": f"Unbalanced braces in @{etype} entry"})
            pos = m.end()
            continue
        pos = i
        if etype in ("comment", "preamble", "string"):
            continue
        key, _, rest = body.partition(",")
        key = key.strip()
        fields, ferr = _parse_fields(rest)
        for e in ferr:
            errors.append({"line": line, "key": key, "message": e})
        entries.append({"type": etype, "key": key, "fields": fields, "line": line,
                        "raw": text[m.start():i]})
    return {"entries": entries, "errors": errors}


def _parse_fields(s: str) -> tuple[dict, list]:
    fields, errs = {}, []
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        m = re.match(r"([A-Za-z][\w\-:]*)\s*=\s*", s[i:])
        if not m:
            nxt = s.find(",", i)
            errs.append(f"Malformed field near: {s[i:i + 30].strip()!r}")
            if nxt == -1:
                break
            i = nxt + 1
            continue
        name = m.group(1).lower()
        i += m.end()
        value, i = _read_value(s, i)
        fields[name] = re.sub(r"\s+", " ", value).strip()
    return fields, errs


def _read_value(s: str, i: int) -> tuple[str, int]:
    parts = []
    n = len(s)
    while i < n:
        c = s[i]
        if c == "{":
            depth, j = 1, i + 1
            while j < n and depth:
                depth += {"{": 1, "}": -1}.get(s[j], 0)
                j += 1
            parts.append(s[i + 1:j - 1])
            i = j
        elif c == '"':
            j = i + 1
            depth = 0
            while j < n and not (s[j] == '"' and depth == 0):
                depth += {"{": 1, "}": -1}.get(s[j], 0)
                j += 1
            parts.append(s[i + 1:j])
            i = j + 1
        else:
            m = re.match(r"[^,#\s}]+", s[i:])
            if m:
                parts.append(m.group(0))
                i += m.end()
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i < n and s[i] == "#":
            i += 1
            while i < n and s[i] in " \t\r\n":
                i += 1
            continue
        break
    return "".join(parts), i


def serialize_entry(e: dict) -> str:
    lines = [f"@{e['type']}{{{e['key']},"]
    for k, v in e["fields"].items():
        lines.append(f"  {k} = {{{v}}},")
    lines.append("}")
    return "\n".join(lines)


def serialize(entries: list[dict]) -> str:
    return "\n\n".join(serialize_entry(e) for e in entries) + ("\n" if entries else "")


def cited_keys(text: str) -> list[str]:
    """Keys in order of first appearance: Pandoc [@a; @b], bare @a, and LaTeX \\cite{a,b}."""
    seen: dict[str, None] = {}
    # Markdown: only treat @key as a citation when inside [...] or preceded by whitespace/start (not emails)
    for m in re.finditer(r"\[([^\]]*@[^\]]*)\]", text):
        for k in CITE_MD.findall(m.group(1)):
            seen.setdefault(k, None)
    for m in re.finditer(r"(?:^|(?<=\s))@([A-Za-z](?:[\w:\-]*\w)?)", text):
        seen.setdefault(m.group(1), None)
    for m in CITE_TEX.finditer(text):
        for k in m.group(1).split(","):
            if k.strip():
                seen.setdefault(k.strip(), None)
    return list(seen)


def authors_list(field: str) -> list[str]:
    return [a.strip() for a in re.split(r"\s+and\s+", field or "") if a.strip()]


def surname(author: str) -> str:
    author = author.strip("{} ")
    if "," in author:
        return author.split(",")[0].strip("{} ")
    return author.split()[-1].strip("{} ") if author.split() else author
