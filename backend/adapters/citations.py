"""Reference & citation verification: RefChecker, CiteScan, check-citations, Paper Analysis Utility,
plus the Step-16 source-overlap detector."""
from __future__ import annotations

import difflib
import re
import time
import unicodedata
import xml.etree.ElementTree as ET

import httpx

from .. import config
from ..netstatus import is_online
from .base import Adapter, result, shingles, split_sentences, strip_markup, words
from .bibtex import authors_list, cited_keys, parse_bib, serialize, surname

UA = {"User-Agent": f"VU3INI-ResearchMate/1.0 (mailto:{config.CONTACT_EMAIL})"}


def norm_title(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"[{}\\]", "", t.lower())).strip()


def title_sim(a: str, b: str) -> float:
    return round(difflib.SequenceMatcher(None, norm_title(a), norm_title(b)).ratio(), 3)


# ======================================================================= RefChecker
class RefCheckerAdapter(Adapter):
    key = "refchecker"
    name = "RefChecker"
    purpose = "Validates every reference (title, authors, venue, year, DOI) against Crossref, Semantic Scholar, OpenAlex and arXiv."
    strengths = "Catches hallucinated or garbled citations before reviewers do."
    repo = "https://github.com/markrussinovich/refchecker"

    def native_available(self) -> bool:
        return is_online()

    def native_version(self) -> str:
        return "online databases reachable"

    def check(self, bib_text: str, client: httpx.Client | None = None) -> dict:
        entries = parse_bib(bib_text)["entries"]
        online = is_online()
        own = client is None
        client = client or httpx.Client(timeout=12, headers=UA, follow_redirects=True)
        out = []
        try:
            for e in entries:
                out.append(self.check_entry(e, client) if online else
                           {"key": e["key"], "status": "unverified-offline", "message": "Offline — will verify when online.",
                            "title": e["fields"].get("title", "")})
        finally:
            if own:
                client.close()
        summary = {}
        for r in out:
            summary[r["status"]] = summary.get(r["status"], 0) + 1
        return result(self.name, "online" if online else "offline", references=out, summary=summary, online=online)

    def check_entry(self, e: dict, client: httpx.Client) -> dict:
        f = e["fields"]
        title, doi, year = f.get("title", ""), f.get("doi", "").strip(), f.get("year", "")
        first = surname(authors_list(f.get("author", ""))[0]) if f.get("author") else ""
        candidates = []
        for fn in (self._crossref, self._openalex, self._s2, self._arxiv):
            try:
                c = fn(client, title, doi)
                if c:
                    candidates.append(c)
                    if c["sim"] >= 0.95 and (not doi or c.get("doi", "").lower() == doi.lower()):
                        break
            except (httpx.HTTPError, ValueError, KeyError, ET.ParseError):
                continue
            time.sleep(0.05)
        if not candidates:
            return {"key": e["key"], "title": title, "status": "not-found",
                    "message": "No matching record in Crossref, OpenAlex, Semantic Scholar or arXiv — possibly hallucinated."}
        best = max(candidates, key=lambda c: c["sim"])
        problems = []
        if best["sim"] < 0.9:
            problems.append(f"title differs (similarity {best['sim']}) — database: “{best['title']}”")
        if year and best.get("year") and str(best["year"]) != str(year).strip()[:4]:
            problems.append(f"year {year} ≠ {best['year']}")
        if first and best.get("authors") and not any(norm_title(first) in norm_title(a) for a in best["authors"][:6]):
            problems.append(f"first author “{first}” not in {', '.join(best['authors'][:3])}")
        if doi and best.get("doi") and doi.lower() != best["doi"].lower():
            problems.append(f"DOI {doi} ≠ {best['doi']}")
        status = "verified" if not problems else "suspicious" if best["sim"] >= 0.75 else "mismatch"
        return {"key": e["key"], "title": title, "status": status, "source": best["db"],
                "matched": {k: best.get(k) for k in ("title", "year", "doi", "venue", "authors")},
                "message": "; ".join(problems) or f"Verified via {best['db']}."}

    @staticmethod
    def _crossref(c, title, doi):
        if doi:
            r = c.get(f"https://api.crossref.org/works/{doi}")
            if r.status_code == 200:
                m = r.json()["message"]
                return _cr(m, title)
        r = c.get("https://api.crossref.org/works", params={"query.bibliographic": title, "rows": 3})
        items = r.json()["message"]["items"] if r.status_code == 200 else []
        best = max((_cr(m, title) for m in items), key=lambda x: x["sim"], default=None)
        return best

    @staticmethod
    def _openalex(c, title, doi):
        url = f"https://api.openalex.org/works/https://doi.org/{doi}" if doi else "https://api.openalex.org/works"
        r = c.get(url, params=None if doi else {"search": title, "per-page": 3})
        if r.status_code != 200:
            return None
        items = [r.json()] if doi else r.json().get("results", [])
        out = [{"db": "OpenAlex", "title": w.get("title") or "", "year": w.get("publication_year"),
                "doi": (w.get("doi") or "").replace("https://doi.org/", ""),
                "venue": ((w.get("primary_location") or {}).get("source") or {}).get("display_name", ""),
                "authors": [a["author"]["display_name"] for a in w.get("authorships", [])],
                "sim": title_sim(title, w.get("title") or "")} for w in items]
        return max(out, key=lambda x: x["sim"], default=None)

    @staticmethod
    def _s2(c, title, doi):
        fields = "title,year,authors,externalIds,venue"
        if doi:
            r = c.get(f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}", params={"fields": fields})
            items = [r.json()] if r.status_code == 200 else []
        else:
            r = c.get("https://api.semanticscholar.org/graph/v1/paper/search", params={"query": title, "limit": 3,
                                                                                     "fields": fields})
            items = r.json().get("data", []) if r.status_code == 200 else []
        out = [{"db": "Semantic Scholar", "title": p.get("title") or "", "year": p.get("year"),
                "doi": (p.get("externalIds") or {}).get("DOI", ""), "venue": p.get("venue", ""),
                "authors": [a["name"] for a in p.get("authors", [])], "sim": title_sim(title, p.get("title") or "")}
               for p in items]
        return max(out, key=lambda x: x["sim"], default=None)

    @staticmethod
    def _arxiv(c, title, doi):
        r = c.get("https://export.arxiv.org/api/query", params={"search_query": f'ti:"{title[:200]}"', "max_results": 3})
        if r.status_code != 200:
            return None
        ns = {"a": "http://www.w3.org/2005/Atom"}
        out = []
        for e in ET.fromstring(r.text).findall("a:entry", ns):
            t = " ".join((e.findtext("a:title", "", ns) or "").split())
            out.append({"db": "arXiv", "title": t, "year": (e.findtext("a:published", "", ns) or "")[:4],
                        "doi": "", "venue": "arXiv",
                        "authors": [a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)],
                        "sim": title_sim(title, t)})
        return max(out, key=lambda x: x["sim"], default=None)


def _cr(m: dict, title: str) -> dict:
    t = (m.get("title") or [""])[0]
    yr = None
    for k in ("published-print", "published-online", "issued", "created"):
        parts = (m.get(k) or {}).get("date-parts") or [[None]]
        if parts and parts[0] and parts[0][0]:
            yr = parts[0][0]
            break
    return {"db": "Crossref", "title": t, "year": yr, "doi": m.get("DOI", ""),
            "venue": (m.get("container-title") or [""])[0],
            "authors": [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in m.get("author", [])],
            "sim": title_sim(title, t)}


# ======================================================================= CiteScan
class CiteScanAdapter(Adapter):
    key = "citescan"
    name = "CiteScan"
    purpose = "Audits 1-to-1 parity between in-text citation keys and .bib entries; one-click removal of unused entries."
    strengths = "Manuscript-aware: knows which references you actually cite."
    repo = "https://github.com/aivolcano/CiteScan"

    def native_available(self) -> bool:
        return True

    def native_version(self) -> str:
        return "built-in parity engine"

    def scan(self, manuscript: str, bib_text: str) -> dict:
        cited = cited_keys(manuscript)
        bib_keys = [e["key"] for e in parse_bib(bib_text)["entries"]]
        bset = set(bib_keys)
        missing = [k for k in cited if k not in bset]
        unused = [k for k in bib_keys if k not in set(cited)]
        return result(self.name, "builtin", cited=cited, bib_keys=bib_keys, missing_in_bib=missing,
                      unused_in_bib=unused, parity=not missing and not unused)

    @staticmethod
    def clean(manuscript: str, bib_text: str) -> dict:
        cited = set(cited_keys(manuscript))
        entries = parse_bib(bib_text)["entries"]
        kept = [e for e in entries if e["key"] in cited]
        removed = [e["key"] for e in entries if e["key"] not in cited]
        return {"bib": serialize(kept), "removed": removed, "kept": [e["key"] for e in kept]}


# ======================================================================= check-citations
REQUIRED = {"article": [["author"], ["title"], ["journal", "journaltitle"], ["year", "date"]],
            "inproceedings": [["author"], ["title"], ["booktitle"], ["year", "date"]],
            "conference": [["author"], ["title"], ["booktitle"], ["year", "date"]],
            "book": [["author", "editor"], ["title"], ["publisher"], ["year", "date"]],
            "incollection": [["author"], ["title"], ["booktitle"], ["publisher"], ["year", "date"]],
            "phdthesis": [["author"], ["title"], ["school", "institution"], ["year", "date"]],
            "mastersthesis": [["author"], ["title"], ["school", "institution"], ["year", "date"]],
            "techreport": [["author"], ["title"], ["institution"], ["year", "date"]],
            "misc": [["title"]], "online": [["title"], ["url"]]}


class CheckCitationsAdapter(Adapter):
    key = "check_citations"
    name = "check-citations"
    purpose = "Static BibTeX validator: syntax, duplicate keys, missing required fields, broken cross-references."
    strengths = "Fast, offline, catches errors that break Pandoc/LaTeX builds."
    repo = "(set URL in scripts/vendor_manifest.json)"

    def native_available(self) -> bool:
        return True

    def native_version(self) -> str:
        return "built-in validator"

    def validate(self, bib_text: str) -> dict:
        parsed = parse_bib(bib_text)
        problems = [{"key": e.get("key", ""), "line": e["line"], "severity": "error", "message": e["message"]}
                    for e in parsed["errors"]]
        seen: dict[str, int] = {}
        keys = {e["key"] for e in parsed["entries"]}
        for e in parsed["entries"]:
            k, f = e["key"], e["fields"]
            if not k:
                problems.append({"key": "", "line": e["line"], "severity": "error", "message": "Entry has no key."})
            elif k in seen:
                problems.append({"key": k, "line": e["line"], "severity": "error",
                                 "message": f"Duplicate key (first defined on line {seen[k]})."})
            else:
                seen[k] = e["line"]
            for group in REQUIRED.get(e["type"], [["title"]]):
                if not any(f.get(g) for g in group):
                    problems.append({"key": k, "line": e["line"], "severity": "error",
                                     "message": f"@{e['type']} missing required field: {' or '.join(group)}."})
            if f.get("crossref") and f["crossref"] not in keys:
                problems.append({"key": k, "line": e["line"], "severity": "error",
                                 "message": f"crossref → '{f['crossref']}' does not exist."})
            if f.get("doi") and not re.match(r"^10\.\d{4,9}/\S+$", f["doi"].strip()):
                problems.append({"key": k, "line": e["line"], "severity": "warning", "message": f"Malformed DOI '{f['doi']}'."})
            if f.get("year") and not re.match(r"^\d{4}[a-z]?$", f["year"].strip()):
                problems.append({"key": k, "line": e["line"], "severity": "warning", "message": f"Year '{f['year']}' is not 4 digits."})
            if f.get("title") and f["title"].count("{") != f["title"].count("}"):
                problems.append({"key": k, "line": e["line"], "severity": "error", "message": "Unbalanced braces in title."})
        return result(self.name, "builtin", entries=len(parsed["entries"]), problems=problems,
                      valid=not any(p["severity"] == "error" for p in problems))


# ======================================================================= Paper Analysis Utility
NUM_RE = re.compile(r"(?<![\w.])(?:p\s*[<=>]\s*)?\d+(?:[.,]\d+)?\s*%?")


class PaperAnalysisAdapter(Adapter):
    key = "paper_analysis"
    name = "Paper Analysis Utility"
    purpose = "Cross-section consistency auditor: numbers, figures, tables and acronyms between Abstract and Results."
    strengths = "Catches the Abstract saying 87 % while Results say 85 %."
    repo = "(set URL in scripts/vendor_manifest.json)"

    def native_available(self) -> bool:
        return True

    def native_version(self) -> str:
        return "built-in auditor"

    def audit(self, sections: list[dict]) -> dict:
        by = {s["name"].lower(): s.get("content") or "" for s in sections}
        abstract = next((v for k, v in by.items() if k.startswith("abstract")), "")
        results = "\n".join(v for k, v in by.items() if k.startswith(("result", "finding", "evaluation", "experiment")))
        body = "\n".join(v for k, v in by.items() if not k.startswith("abstract"))
        issues = []

        def nums(t):
            return {re.sub(r"\s+", "", n.group(0)).replace(",", "."): n.group(0) for n in NUM_RE.finditer(t)}
        a_nums, r_nums, b_nums = nums(abstract), nums(results), nums(body)
        for n in a_nums:
            if re.fullmatch(r"(19|20)\d\d", n) or n in ("1", "2", "3"):
                continue
            if n not in (r_nums or b_nums):
                where = "Results" if results else "the body"
                issues.append({"kind": "number", "severity": "error",
                               "message": f"Abstract reports “{a_nums[n].strip()}” but this value does not appear in {where}."})
        full = "\n".join(s.get("content") or "" for s in sections)
        defined = {m.group(2): m.start() for m in re.finditer(r"([A-Z][a-z]+(?:[ -][A-Za-z][a-z]+)+)\s+\(([A-Z][A-Za-z0-9]{1,9})\)", full)}
        for m in re.finditer(r"\b([A-Z][A-Z0-9]{1,9}s?)\b", full):
            ac = m.group(1).rstrip("s") if m.group(1).endswith("s") and m.group(1)[:-1].isupper() else m.group(1)
            if ac in ("I", "A", "AND", "OR", "THE", "AI", "DOI", "URL") or len(ac) < 2:
                continue
            if ac not in defined:
                issues.append({"kind": "acronym", "severity": "warning", "message": f"Acronym {ac} is never defined."})
                defined[ac] = -1
            elif 0 <= m.start() < defined[ac] and not full[m.start() - 1:m.start()] == "(":
                issues.append({"kind": "acronym", "severity": "warning",
                               "message": f"Acronym {ac} is used before it is defined."})
                defined[ac] = -1
        for kind in ("Figure", "Table"):
            refs = {int(n) for n in re.findall(rf"\b{kind[:3]}\w*\.?\s+(\d+)", full)}
            caps = {int(n) for n in re.findall(rf"^\s*\**{kind}\s+(\d+)[:.]", full, re.M)}
            for n in sorted(refs - caps) if caps else []:
                issues.append({"kind": kind.lower(), "severity": "error", "message": f"{kind} {n} is referenced but has no caption."})
            for n in sorted(caps - refs):
                issues.append({"kind": kind.lower(), "severity": "warning", "message": f"{kind} {n} has a caption but is never referenced."})
            if refs and sorted(refs) != list(range(1, max(refs) + 1)):
                issues.append({"kind": kind.lower(), "severity": "warning",
                               "message": f"{kind} numbering is not consecutive: {sorted(refs)}."})
        return result(self.name, "builtin", issues=issues, abstract_numbers=sorted(a_nums),
                      results_numbers=sorted(r_nums), consistent=not any(i["severity"] == "error" for i in issues))


# ======================================================================= Step 16 — overlap
def source_overlap(manuscript: str, sources: list[dict], n: int = 5) -> dict:
    src_shingles = [(s["filename"], shingles(s.get("text", ""), n), s.get("text", "")) for s in sources]
    flagged = []
    for sent in split_sentences(manuscript):
        plain = strip_markup(sent["text"])
        if sent["text"].strip().startswith(">") or re.search(r"[“\"][^”\"]{25,}[”\"]", sent["text"]):
            continue  # already a direct quotation
        sh = shingles(plain, n)
        if len(sh) < 3:
            continue
        for name, ss, raw in src_shingles:
            if not ss:
                continue
            containment = len(sh & ss) / len(sh)
            if containment >= 0.35:
                kind = "verbatim" if containment >= 0.8 else "close-paraphrase"
                sm = difflib.SequenceMatcher(None, words(plain), words(raw), autojunk=False)
                blk = max(sm.get_matching_blocks(), key=lambda b: b.size)
                match_text = " ".join(words(raw)[blk.b:blk.b + blk.size])
                flagged.append({"sentence": sent["text"], "start": sent["start"], "end": sent["end"], "source": name,
                                "containment": round(containment, 3), "kind": kind, "matched_words": match_text[:400],
                                "advice": "Put in quotation marks with a citation, or rewrite in your own words."
                                if kind == "verbatim" else "Close paraphrase — restructure the sentence and cite the source."})
                break
    return {"tool": "Source Overlap", "engine": "builtin", "ok": True, "flagged": flagged,
            "sentences_checked": len(split_sentences(manuscript))}
