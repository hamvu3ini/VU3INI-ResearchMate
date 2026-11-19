"""Typst (instant live preview) and Pandoc + citeproc (final DOCX / LaTeX / PDF export in any CSL style)."""
from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from .. import config
from .base import Adapter, cli_version, result, run_cli
from .bibtex import cited_keys, parse_bib, serialize
from .render import CSL_STYLES, apply_citations, md_to_html, md_to_latex, md_to_typst

PREVIEW_CSS = ("body{font-family:Georgia,'Times New Roman',serif;max-width:46rem;margin:2rem auto;padding:0 1rem;"
               "line-height:1.6;color:#1a202c;background:#fff}h1{text-align:center;font-size:1.6rem}"
               "h2{font-size:1.2rem;margin-top:1.6rem;border-bottom:1px solid #ddd}"
               "blockquote{border-left:3px solid #2B6CB0;margin:0;padding-left:1rem;color:#4a5568}"
               ".refs p{padding-left:2em;text-indent:-2em;margin:.3em 0}")


def csl_path(style: str) -> Path | None:
    p = config.VENDOR_CSL / f"{style}.csl"
    return p if p.exists() else None


def html_document(md: str, bib: str, style: str, title: str) -> str:
    body, refs = apply_citations(md, bib, style)
    ref_html = "".join(f"<p>{md_to_html(r)[3:-4]}</p>" for r in refs)
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{escape(title)}</title><style>{PREVIEW_CSS}"
            f"</style></head><body>{md_to_html(body)}" +
            (f"<h2>References</h2><div class='refs'>{ref_html}</div>" if refs else "") + "</body></html>")


class TypstAdapter(Adapter):
    key = "typst"
    name = "Typst"
    purpose = "Instant live PDF/SVG preview — a fast LaTeX alternative while writing."
    strengths = "Compiles in milliseconds; what you see is close to the final typeset page."
    repo = "https://github.com/typst/typst"

    def binary(self):
        return config.find_binary("typst")

    def native_available(self) -> bool:
        return bool(self.binary())

    def native_version(self) -> str:
        return cli_version(self.binary(), "--version")

    def preview(self, md: str, bib: str, style: str, title: str) -> dict:
        exe = self.binary()
        if exe:
            try:
                with tempfile.TemporaryDirectory() as td:
                    tdp = Path(td)
                    entries = parse_bib(bib)["entries"]
                    have = {e["key"] for e in entries}
                    # Typst errors on unknown keys — drop citations that have no .bib entry
                    clean = re.sub(r"@([A-Za-z](?:[\w:\-]*\w)?)", lambda m: m.group(0) if m.group(1) in have else "[?]",
                                   md_to_typst(md))
                    (tdp / "refs.bib").write_text(serialize(entries), encoding="utf-8")
                    style_arg = '"style.csl"' if csl_path(style) else f'"{_typst_style(style)}"'
                    if csl_path(style):
                        shutil.copy(csl_path(style), tdp / "style.csl")
                    bibl = f'\n#bibliography("refs.bib", style: {style_arg})\n' if entries and cited_keys(md) else ""
                    (tdp / "main.typ").write_text('#set page(paper: "a4", margin: 2.2cm)\n#set text(size: 11pt)\n'
                                                  '#set par(justify: true)\n#show heading.where(level: 1): set align(center)\n'
                                                  + clean + bibl, encoding="utf-8")
                    proc = run_cli([exe, "compile", "main.typ", "page-{p}.svg"], cwd=td, timeout=60)
                    if proc.returncode != 0:
                        raise RuntimeError(proc.stderr.strip()[:400])
                    pages = sorted(tdp.glob("page-*.svg"), key=lambda p: int(re.findall(r"\d+", p.stem)[0]))
                    return result(self.name, "native", format="svg", pages=[p.read_text(encoding="utf-8") for p in pages])
            except Exception as exc:  # noqa: BLE001
                return result(self.name, "builtin", format="html", html=html_document(md, bib, style, title),
                              note=f"Typst compile failed ({exc}); showing HTML preview.")
        return result(self.name, "builtin", format="html", html=html_document(md, bib, style, title),
                      note="typst.exe not found in vendor/bin; showing built-in HTML preview.")


def _typst_style(style: str) -> str:
    return {"apa": "apa", "ieee": "ieee", "vancouver": "vancouver", "chicago-author-date": "chicago-author-date",
            "nature": "nature", "acm-sig-proceedings": "association-for-computing-machinery"}.get(style, "apa")


def next_version(directory: Path, stem: str, base: str = "1.0.0") -> str:
    """Manuscript_v1.0.0 → if any file with that version exists, bump patch until free."""
    major, minor, patch = (int(x) for x in base.split("."))
    while list(directory.glob(f"{stem}_v{major}.{minor}.{patch}.*")):
        patch += 1
    return f"{major}.{minor}.{patch}"


class PandocAdapter(Adapter):
    key = "pandoc"
    name = "Pandoc + citeproc"
    purpose = "Exports the final manuscript to DOCX, LaTeX (.tex) and PDF in any CSL citation style."
    strengths = "Journal-exact citation formatting from official CSL files; universal format conversion."
    repo = "https://github.com/jgm/pandoc"

    def binary(self):
        return config.find_binary("pandoc")

    def native_available(self) -> bool:
        return bool(self.binary())

    def native_version(self) -> str:
        return cli_version(self.binary(), "--version")

    def export(self, md: str, bib: str, style: str, title: str, fmt: str, out_dir: Path,
               version: str | None = None) -> dict:
        fmt = fmt.lower()
        if fmt not in ("docx", "tex", "pdf"):
            raise ValueError("format must be docx, tex or pdf")
        style = style if style in CSL_STYLES else "apa"
        out_dir.mkdir(parents=True, exist_ok=True)
        version = version or next_version(out_dir, "Manuscript")
        out = out_dir / f"Manuscript_v{version}.{fmt}"
        exe = self.binary()
        if exe:
            try:
                return self._native(exe, md, bib, style, title, fmt, out, version)
            except Exception as exc:  # noqa: BLE001
                res = self._builtin(md, bib, style, title, fmt, out, version)
                res["note"] = f"pandoc failed ({exc}); used built-in exporter. " + res.get("note", "")
                return res
        res = self._builtin(md, bib, style, title, fmt, out, version)
        res["note"] = "pandoc.exe not found in vendor/bin; used built-in exporter (approximate CSL formatting). " \
            + res.get("note", "")
        return res

    def _native(self, exe, md, bib, style, title, fmt, out: Path, version) -> dict:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            body = re.sub(r"^# .*\n", "", md, count=1)
            front = f"---\ntitle: \"{title.replace(chr(34), '')}\"\n---\n\n"
            (tdp / "m.md").write_text(front + body, encoding="utf-8")
            (tdp / "refs.bib").write_text(bib or "", encoding="utf-8")
            cmd = [exe, "m.md", "--citeproc", "-o", str(out)]
            if parse_bib(bib)["entries"]:
                cmd += ["--bibliography", "refs.bib"]
            if csl_path(style):
                cmd += ["--csl", str(csl_path(style))]
            if fmt == "tex":
                cmd += ["-s", "-t", "latex"]
            if fmt == "pdf":
                engine = config.find_binary("typst") and "typst" or config.find_binary("xelatex") and "xelatex" \
                    or config.find_binary("pdflatex") and "pdflatex"
                if not engine:
                    raise RuntimeError("no PDF engine (typst/xelatex) available")
                cmd += [f"--pdf-engine={config.find_binary(engine)}"]
            proc = run_cli(cmd, cwd=td, timeout=300)
            if proc.returncode != 0 or not out.exists():
                raise RuntimeError(proc.stderr.strip()[:400] or "pandoc produced no output")
        note = "" if csl_path(style) else (f"{style}.csl is not in vendor/csl, so pandoc used its default "
                                            "Chicago author-date style. Run build_installer.bat to vendor CSL styles.")
        return result(self.name, "native", path=str(out), filename=out.name, version=version, style=style, format=fmt,
                      note=note)

    def _builtin(self, md, bib, style, title, fmt, out: Path, version) -> dict:
        body, refs = apply_citations(md, bib, style)
        note = ""
        if fmt == "tex":
            out.write_text(md_to_latex(body, title, refs), encoding="utf-8")
        elif fmt == "docx":
            write_docx(out, body, refs)
        else:
            html_doc = html_document(md, bib, style, title)
            try:
                html_to_pdf(html_doc, out)
            except ImportError:
                return {"tool": self.name, "engine": "builtin", "ok": False,
                        "error": "PDF export needs pandoc+typst, or PyMuPDF installed from vendor/wheels."}
            note = "PDF rendered with PyMuPDF."
        return result(self.name, "builtin", path=str(out), filename=out.name, version=version, style=style,
                      format=fmt, note=note)


def html_to_pdf(html_doc: str, out: Path) -> None:
    import fitz  # PyMuPDF
    body = re.search(r"<body>(.*)</body>", html_doc, re.S).group(1)
    css = "body{font-family:serif;font-size:11pt;line-height:1.45}h1{font-size:16pt;text-align:center}" \
          "h2{font-size:13pt;margin-top:12pt}p{margin:0 0 6pt 0;text-align:justify}"
    story = fitz.Story(html=body, user_css=css)
    writer = fitz.DocumentWriter(str(out))
    rect, where = fitz.paper_rect("a4"), fitz.paper_rect("a4") + (60, 60, -60, -60)
    more = True
    while more:
        dev = writer.begin_page(rect)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()


def _runs(text: str) -> str:
    out = []
    for part in re.split(r"(\*\*.+?\*\*|\*[^*]+?\*|<sup>.+?</sup>)", text):
        if not part:
            continue
        props = ""
        if part.startswith("**"):
            part, props = part[2:-2], "<w:rPr><w:b/></w:rPr>"
        elif part.startswith("<sup>"):
            part, props = part[5:-6], '<w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
        elif part.startswith("*"):
            part, props = part[1:-1], "<w:rPr><w:i/></w:rPr>"
        out.append(f'<w:r>{props}<w:t xml:space="preserve">{escape(part)}</w:t></w:r>')
    return "".join(out)


def write_docx(out: Path, md: str, refs: list[str]) -> None:
    """Minimal but valid OOXML document with Title/Heading styles (opens in Word, LibreOffice, Google Docs)."""
    paras = []
    buf: list[str] = []

    def flush():
        if buf:
            paras.append(f"<w:p>{_runs(' '.join(buf))}</w:p>")
            buf.clear()
    for line in md.splitlines():
        h = re.match(r"^(#{1,6})\s+(.*)", line.strip())
        if h:
            flush()
            st = "Title" if len(h.group(1)) == 1 else f"Heading{len(h.group(1)) - 1}"
            paras.append(f'<w:p><w:pPr><w:pStyle w:val="{st}"/></w:pPr>{_runs(h.group(2))}</w:p>')
        elif not line.strip():
            flush()
        else:
            buf.append(line.strip())
    flush()
    if refs:
        paras.append('<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>References</w:t></w:r></w:p>')
        paras += [f'<w:p><w:pPr><w:ind w:left="720" w:hanging="720"/></w:pPr>{_runs(r)}</w:p>' for r in refs]
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {W}><w:body>{"".join(paras)}' \
          '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" ' \
          'w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>'

    def style(sid, name, size, bold=True, center=False):
        jc = '<w:jc w:val="center"/>' if center else ""
        b = "<w:b/>" if bold else ""
        return (f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/><w:basedOn w:val="Normal"/>'
                f'<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/>'
                f'{jc}</w:pPr><w:rPr>{b}'
                f'<w:sz w:val="{size}"/></w:rPr></w:style>')
    styles = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {W}><w:docDefaults><w:rPrDefault>'
              '<w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman"/><w:sz w:val="24"/></w:rPr>'
              '</w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="300" w:lineRule="auto"/>'
              '</w:pPr></w:pPrDefault></w:docDefaults><w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
              '<w:name w:val="Normal"/><w:qFormat/></w:style>' + style("Title", "Title", 36, center=True) +
              style("Heading1", "heading 1", 28) + style("Heading2", "heading 2", 26) + style("Heading3", "heading 3", 24) +
              "</w:styles>")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns='
                   '"http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" '
                   'ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" '
                   'ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/'
                   'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName='
                   '"/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
                   'styles+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns='
                   '"http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type='
                   '"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target='
                   '"word/document.xml"/></Relationships>')
        z.writestr("word/_rels/document.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship '
                   'Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                   'Target="styles.xml"/></Relationships>')
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", styles)
