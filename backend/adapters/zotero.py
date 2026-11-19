"""Zotero + Better BibTeX — live library/collection export via BBT's local HTTP endpoints (port 23119)."""
from __future__ import annotations

from urllib.parse import quote

import httpx

from .base import Adapter, result
from .bibtex import parse_bib

BBT = "http://127.0.0.1:23119/better-bibtex"


class ZoteroAdapter(Adapter):
    key = "zotero"
    name = "Zotero + Better BibTeX"
    purpose = "Live sync of Zotero collections as stable-key .bib via Better BibTeX."
    strengths = "Citation keys stay stable while you keep adding papers in Zotero."
    repo = "https://github.com/retorquere/zotero-better-bibtex"

    def native_available(self) -> bool:
        try:
            r = httpx.get(BBT + "/cayw", params={"probe": "true"}, timeout=1)
            return r.status_code == 200 and "ready" in r.text
        except httpx.HTTPError:
            return False

    def native_version(self) -> str:
        return "Better BibTeX ready"

    def export(self, collection: str = "", library_id: int = 1, fmt: str = "bibtex") -> dict:
        if not self.native_available():
            return {"tool": self.name, "engine": "native", "ok": False,
                    "error": "Zotero is not running or Better BibTeX is not installed. Start Zotero, or import a .bib file."}
        path = (f"/export/collection?/{library_id}/{quote(collection)}.{fmt}" if collection
                else f"/export/library?/{library_id}/library.{fmt}")
        r = httpx.get(BBT + path, timeout=60)
        if r.status_code != 200:
            return {"tool": self.name, "engine": "native", "ok": False,
                    "error": f"Better BibTeX returned {r.status_code}: {r.text[:200]}"}
        parsed = parse_bib(r.text)
        return result(self.name, "native", bib=r.text, entries=len(parsed["entries"]), errors=parsed["errors"])

    def collections(self) -> list[dict]:
        """Collections of the local library via Zotero 7's local API (enabled by ResearchMate's Zotero setup).
        Returns [{'key', 'name', 'path'}] where path is 'Parent/Child' — the form Better BibTeX export expects."""
        try:
            items, start = [], 0
            while True:
                r = httpx.get("http://127.0.0.1:23119/api/users/0/collections",
                              params={"limit": 100, "start": start}, timeout=10)
                if r.status_code != 200:
                    return []
                page = r.json()
                items += page
                if len(page) < 100:
                    break
                start += 100
        except (httpx.HTTPError, ValueError):
            return []
        by_key = {c["key"]: c.get("data", c) for c in items}

        def path(key: str, depth: int = 0) -> str:
            d = by_key[key]
            parent = d.get("parentCollection")
            return (path(parent, depth + 1) + "/" if parent and parent in by_key and depth < 20 else "") + d["name"]
        return sorted(({"key": k, "name": d["name"], "path": path(k)} for k, d in by_key.items()),
                      key=lambda c: c["path"].lower())

    def rpc(self, method: str, params: list | None = None) -> dict:
        r = httpx.post(BBT + "/json-rpc", json={"jsonrpc": "2.0", "method": method, "params": params or [], "id": 1},
                       timeout=20)
        return r.json()
