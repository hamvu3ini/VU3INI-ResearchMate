"""GROBID — ML PDF → TEI/XML parser (service on 127.0.0.1:8070). Fallback: PyMuPDF / pypdf text with
font-size and heading-pattern section detection."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from .base import Adapter, result

GROBID_URL = "http://127.0.0.1:8070"
TEI = "{http://www.tei-c.org/ns/1.0}"
HEADINGS = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?(abstract|introduction|background|related work|literature review|"
                      r"method(?:s|ology)?|materials and methods|experiment(?:s|al setup)?|results?|discussion|"
                      r"conclusions?|limitations|references|bibliography|acknowledg(?:e)?ments?)\s*$", re.I)


class GrobidAdapter(Adapter):
    key = "grobid"
    name = "GROBID"
    purpose = "ML-powered PDF → TEI/XML parser: sections, metadata, tables and references."
    strengths = "Structural accuracy — knows where Methods ends and References begin."
    repo = "https://github.com/kermitt2/grobid"

    def native_available(self) -> bool:
        try:
            return httpx.get(GROBID_URL + "/api/isalive", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    def native_version(self) -> str:
        try:
            return "GROBID " + httpx.get(GROBID_URL + "/api/version", timeout=1).text.strip()
        except httpx.HTTPError:
            return "GROBID"

    def parse(self, path: str) -> dict:
        p = Path(path)
        if p.suffix.lower() == ".pdf" and self.native_available():
            try:
                with open(p, "rb") as fh:
                    r = httpx.post(GROBID_URL + "/api/processFulltextDocument", files={"input": fh},
                                   data={"consolidateHeader": "0"}, timeout=300)
                r.raise_for_status()
                secs, refs, title = self.parse_tei(r.text)
                text = "\n\n".join(f"{s['head']}\n{s['text']}" for s in secs)
                return result(self.name, "native", title=title, sections=secs, references=refs, tei=r.text, text=text)
            except (httpx.HTTPError, ET.ParseError) as exc:
                fb = self.fallback(p)
                fb["note"] = f"GROBID failed ({exc}); used PyMuPDF fallback."
                return fb
        fb = self.fallback(p)
        if p.suffix.lower() == ".pdf":
            fb["note"] = "GROBID service not running; used lightweight PyMuPDF/pypdf fallback."
        return fb

    @staticmethod
    def parse_tei(xml: str) -> tuple[list, list, str]:
        root = ET.fromstring(xml)
        title_el = root.find(f".//{TEI}titleStmt/{TEI}title")
        title = (title_el.text or "").strip() if title_el is not None else ""
        secs = []
        abstract = root.find(f".//{TEI}profileDesc/{TEI}abstract")
        if abstract is not None:
            secs.append({"head": "Abstract", "text": " ".join("".join(x.itertext()).strip() for x in abstract.iter(f"{TEI}p"))})
        for div in root.iterfind(f".//{TEI}body/{TEI}div"):
            head = div.find(f"{TEI}head")
            paras = [" ".join("".join(pp.itertext()).split()) for pp in div.iterfind(f"{TEI}p")]
            secs.append({"head": (head.text or "").strip() if head is not None else "Section", "text": "\n".join(paras)})
        refs = []
        for b in root.iterfind(f".//{TEI}listBibl/{TEI}biblStruct"):
            t = b.find(f".//{TEI}title")
            refs.append({"title": "".join(t.itertext()).strip() if t is not None else "",
                         "doi": next((i.text for i in b.iterfind(f".//{TEI}idno") if i.get("type") == "DOI"), "")})
        return secs, refs, title

    def fallback(self, p: Path) -> dict:
        lines: list[tuple[str, float]] = []
        engine_note = ""
        if p.suffix.lower() == ".pdf":
            try:
                import fitz  # PyMuPDF
                with fitz.open(str(p)) as doc:
                    for page in doc:
                        for block in page.get_text("dict")["blocks"]:
                            for line in block.get("lines", []):
                                txt = "".join(s["text"] for s in line["spans"]).strip()
                                size = max((s["size"] for s in line["spans"]), default=10)
                                if txt:
                                    lines.append((txt, size))
                engine_note = "PyMuPDF"
            except ImportError:
                try:
                    from pypdf import PdfReader
                    for pg in PdfReader(str(p)).pages:
                        lines += [(ln, 10.0) for ln in (pg.extract_text() or "").splitlines() if ln.strip()]
                    engine_note = "pypdf"
                except ImportError:
                    return {"tool": self.name, "engine": "builtin", "ok": False,
                            "error": "No PDF parser available (install PyMuPDF from vendor/wheels)."}
        else:
            lines = [(ln, 10.0) for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()]
            engine_note = "plain text"
        body_size = sorted(s for _, s in lines)[len(lines) // 2] if lines else 10
        secs, cur = [], {"head": "Front matter", "text": []}
        for txt, size in lines:
            md = txt.lstrip("# ").strip() if txt.startswith("#") else txt
            if HEADINGS.match(md) or (txt.startswith("#")) or (size >= body_size + 1.5 and len(txt) < 80):
                if cur["text"]:
                    secs.append({"head": cur["head"], "text": " ".join(cur["text"])})
                cur = {"head": md, "text": []}
            else:
                cur["text"].append(txt)
        if cur["text"]:
            secs.append({"head": cur["head"], "text": " ".join(cur["text"])})
        title = lines[0][0] if lines else p.stem
        text = "\n\n".join(f"{s['head']}\n{s['text']}" for s in secs)
        return result(self.name, "builtin", title=title, sections=secs, references=[], tei="", text=text,
                      parser=engine_note)
