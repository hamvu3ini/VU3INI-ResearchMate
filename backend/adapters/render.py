"""Manuscript assembly + a small citeproc-style formatter and Markdown→HTML renderer.

Used by the Typst preview fallback and the Pandoc export fallback so previews/exports still work
when typst.exe / pandoc.exe are not present. Pandoc+citeproc with real CSL files remains the
reference path whenever pandoc is available."""
from __future__ import annotations

import html
import re

from .bibtex import authors_list, cited_keys, parse_bib, surname

CSL_STYLES = {
    "apa": "APA 7th", "ieee": "IEEE", "vancouver": "Vancouver", "chicago-author-date": "Chicago (author-date)",
    "nature": "Nature", "acm-sig-proceedings": "ACM SIG Proceedings",
}
NUMERIC = {"ieee", "vancouver", "nature", "acm-sig-proceedings"}


def assemble_markdown(project: dict, sections: list[dict]) -> str:
    parts = [f"# {project['title']}\n"]
    for s in sections:
        body = (s.get("content") or "").strip()
        if body:
            parts.append(f"## {s['name']}\n\n{body}\n")
    return "\n".join(parts)


def _authors_fmt(e: dict, style: str) -> str:
    names = authors_list(e["fields"].get("author", "")) or authors_list(e["fields"].get("editor", ""))
    out = []
    for a in names:
        a = a.strip("{}")
        if "," in a:
            last, first = [x.strip() for x in a.split(",", 1)]
        else:
            bits = a.split()
            last, first = (bits[-1], " ".join(bits[:-1])) if bits else (a, "")
        initials = "".join(f"{p[0]}." for p in re.split(r"[\s\-]+", first) if p)
        if style in ("ieee", "acm-sig-proceedings"):
            out.append(f"{' '.join(i for i in initials.split('.') if i).replace(' ', '. ')}{'.' if initials else ''} {last}".strip())
        elif style in ("vancouver", "nature"):
            out.append(f"{last} {initials.replace('.', '')}".strip())
        else:
            out.append(f"{last}, {' '.join(initials.split('.')).strip().replace(' ', '. ')}{'.' if initials else ''}".strip())
    if not out:
        return ""
    if style in ("vancouver",) and len(out) > 6:
        return ", ".join(out[:6]) + ", et al."
    if len(out) == 1:
        return out[0]
    joiner = " & " if style == "apa" else " and "
    return ", ".join(out[:-1]) + ("," if len(out) > 2 and style != "ieee" else "") + joiner + out[-1]


def format_reference(e: dict, style: str, n: int) -> str:
    f = {k: v.strip("{}") for k, v in e["fields"].items()}
    au, yr, ti = _authors_fmt(e, style), f.get("year", f.get("date", "n.d.")[:4]), f.get("title", "")
    venue = f.get("journal") or f.get("journaltitle") or f.get("booktitle") or f.get("publisher") or ""
    vol, iss, pp = f.get("volume", ""), f.get("number", ""), f.get("pages", "").replace("--", "–")
    doi = f.get("doi", "")
    if style == "apa":
        s = f"{au} ({yr}). {ti}. *{venue}*" + (f", *{vol}*" if vol else "") + (f"({iss})" if iss else "") + \
            (f", {pp}" if pp else "") + "."
        return s + (f" https://doi.org/{doi}" if doi else "")
    if style == "chicago-author-date":
        return f"{au}. {yr}. “{ti}.” *{venue}* {vol}" + (f" ({iss})" if iss else "") + (f": {pp}" if pp else "") + \
            "." + (f" https://doi.org/{doi}." if doi else "")
    if style == "ieee":
        return f"[{n}] {au}, “{ti},” *{venue}*" + (f", vol. {vol}" if vol else "") + (f", no. {iss}" if iss else "") + \
            (f", pp. {pp}" if pp else "") + f", {yr}" + (f", doi: {doi}" if doi else "") + "."
    if style == "acm-sig-proceedings":
        return f"[{n}] {au}. {yr}. {ti}. *{venue}*" + (f" {vol}" if vol else "") + (f", {iss}" if iss else "") + \
            (f" ({yr}), {pp}" if pp else "") + "." + (f" https://doi.org/{doi}" if doi else "")
    if style == "nature":
        return f"{n}. {au}. {ti}. *{venue}* **{vol}**" + (f", {pp}" if pp else "") + f" ({yr})."
    return f"{n}. {au}. {ti}. {venue}. {yr}" + (f";{vol}" if vol else "") + (f"({iss})" if iss else "") + \
        (f":{pp}" if pp else "") + "." + (f" doi:{doi}" if doi else "")


def apply_citations(md: str, bib_text: str, style: str) -> tuple[str, list[str]]:
    """Replace [@key; @key2] / @key with in-text citations and return the reference list."""
    style = style if style in CSL_STYLES else "apa"
    entries = {e["key"]: e for e in parse_bib(bib_text)["entries"]}
    order = [k for k in cited_keys(md) if k in entries]
    num = {k: i + 1 for i, k in enumerate(order)}

    def intext(keys: list[str]) -> str:
        keys = [k for k in keys if k in entries]
        if not keys:
            return "[?]"
        if style in NUMERIC:
            ns = ",".join(str(num[k]) for k in keys)
            return f"<sup>{ns}</sup>" if style == "nature" else f"[{ns}]" if style != "vancouver" else f"({ns})"
        bits = []
        for k in keys:
            names = authors_list(entries[k]["fields"].get("author", ""))
            sn = surname(names[0]) if names else entries[k]["fields"].get("title", k)[:20]
            if len(names) == 2:
                sn += (" & " if style == "apa" else " and ") + surname(names[1])
            elif len(names) > 2:
                sn += " et al."
            bits.append(f"{sn}{',' if style == 'apa' else ''} {entries[k]['fields'].get('year', 'n.d.')}")
        return "(" + "; ".join(bits) + ")"

    md = re.sub(r"\[((?:[^\]]*?@[\w:\-]+)[^\]]*)\]",
                lambda m: intext(re.findall(r"@([\w:\-]*[\w])", m.group(1))), md)
    md = re.sub(r"(?:^|(?<=\s))@([A-Za-z](?:[\w:\-]*\w)?)", lambda m: intext([m.group(1)]), md)
    ordered = order if style in NUMERIC else sorted(order, key=lambda k: _authors_fmt(entries[k], "apa").lower())
    refs = [format_reference(entries[k], style, num[k]) for k in ordered]
    return md, refs


def md_inline(t: str) -> str:
    t = html.escape(t, quote=False).replace("&lt;sup&gt;", "<sup>").replace("&lt;/sup&gt;", "</sup>")
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", t)
    t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    return t


def md_to_html(md: str) -> str:
    out, para, in_list = [], [], False

    def flush():
        nonlocal para
        if para:
            out.append("<p>" + md_inline(" ".join(para)) + "</p>")
            para = []
    for line in md.splitlines():
        s = line.strip()
        h = re.match(r"^(#{1,6})\s+(.*)", s)
        if h:
            flush()
            lvl = len(h.group(1))
            out.append(f"<h{lvl}>{md_inline(h.group(2))}</h{lvl}>")
        elif re.match(r"^[-*]\s+", s):
            flush()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + md_inline(s[2:].strip()) + "</li>")
        elif s.startswith(">"):
            flush()
            out.append("<blockquote>" + md_inline(s.lstrip("> ")) + "</blockquote>")
        elif not s:
            flush()
            if in_list:
                out.append("</ul>")
                in_list = False
        else:
            para.append(s)
    flush()
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def md_to_typst(md: str) -> str:
    """Markdown → Typst markup (headings, emphasis, citations become @key)."""
    lines = []
    for line in md.splitlines():
        h = re.match(r"^(#{1,6})\s+(.*)", line)
        if h:
            lines.append("=" * len(h.group(1)) + " " + h.group(2))
            continue
        line = re.sub(r"[#$<>]", lambda m: "\\" + m.group(0), line)
        line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)
        line = re.sub(r"(?<![*\w])\*(?!\s)([^*]+?)\*(?![*\w])", r"_\1_", line)
        line = re.sub(r"\[((?:[^\]]*?@[\w:\-]+)[^\]]*)\]",
                      lambda m: " ".join("@" + k for k in re.findall(r"@([\w:\-]*[\w])", m.group(1))), line)
        lines.append(line)
    return "\n".join(lines)


def latex_escape(t: str) -> str:
    for a, b in (("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"), ("$", r"\$"), ("#", r"\#"),
                 ("_", r"\_"), ("{", r"\{"), ("}", r"\}"), ("~", r"\textasciitilde{}"), ("^", r"\^{}")):
        t = t.replace(a, b)
    return t


def md_to_latex(md: str, title: str, refs: list[str]) -> str:
    body = []
    for line in md.splitlines():
        h = re.match(r"^(#{1,6})\s+(.*)", line)
        if h:
            if len(h.group(1)) == 1:
                continue
            cmd = {2: "section", 3: "subsection"}.get(len(h.group(1)), "subsubsection")
            body.append(f"\\{cmd}{{{latex_escape(h.group(2))}}}")
            continue
        t = latex_escape(line)
        t = t.replace(r"\textbackslash{}", "\\")  # keep author LaTeX (e.g. \cite) intact
        t = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", t)
        t = re.sub(r"\*(.+?)\*", r"\\emph{\1}", t)
        t = t.replace("<sup>", r"\textsuperscript{").replace("</sup>", "}")
        body.append(t)
    refs_tex = "\n".join(f"\\item {latex_escape(re.sub(r'[*]', '', r))}" for r in refs)
    return ("\\documentclass[11pt]{article}\n\\usepackage[utf8]{inputenc}\n\\usepackage[T1]{fontenc}\n"
            "\\usepackage{hyperref}\n\\usepackage[margin=2.5cm]{geometry}\n"
            f"\\title{{{latex_escape(title)}}}\n\\date{{}}\n\\begin{{document}}\n\\maketitle\n" + "\n".join(body) +
            ("\n\\section*{References}\n\\begin{itemize}\n" + refs_tex + "\n\\end{itemize}\n" if refs else "") +
            "\\end{document}\n")
