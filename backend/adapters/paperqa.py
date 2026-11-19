"""PaperQA2 — citation-grounded RAG verification of claims against your indexed source PDFs.

Native path: the `paper-qa` package (Future-House) configured for local Ollama models.
Built-in path: passage-level BM25 (+ optional dense re-ranking with sentence-transformers on the
compute-plan device) that returns the exact supporting passage and a support verdict for every claim."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .base import BM25, Adapter, content_words, data_root, result, split_sentences, strip_markup


def chunk_passages(text: str, size: int = 3) -> list[str]:
    sents = [s["text"] for s in split_sentences(text)]
    return [" ".join(sents[i:i + size]) for i in range(0, len(sents), max(1, size - 1))] or ([text] if text else [])


class PaperQAAdapter(Adapter):
    key = "paperqa"
    name = "PaperQA2"
    purpose = "RAG claim verification: checks every claim against indexed PDFs and returns evidence passages."
    strengths = "Semantic retrieval with exact quotes — shows *where* support comes from."
    repo = "https://github.com/Future-House/paper-qa"

    def native_available(self) -> bool:
        try:
            import paperqa  # noqa: F401
            return True
        except ImportError:
            return False

    def native_version(self) -> str:
        try:
            import paperqa
            return "paper-qa " + getattr(paperqa, "__version__", "")
        except ImportError:
            return ""

    def index_path(self, pid: int) -> Path:
        return data_root() / "index" / f"project_{pid}.json"

    def build_index(self, pid: int, sources: list[dict], device: str = "cpu") -> dict:
        passages = []
        for s in sources:
            secs = s.get("parsed_sections") or [{"head": "", "text": s.get("text", "")}]
            for sec in secs:
                for chunk in chunk_passages(sec.get("text", "")):
                    if len(content_words(chunk)) >= 4:
                        passages.append({"source_id": s["id"], "source": s["filename"],
                                         "section": sec.get("head", ""), "text": chunk})
        dense = self._embed([p["text"] for p in passages], device) if passages else None
        idx = {"passages": passages, "dense": dense is not None, "device": device if dense is not None else "cpu"}
        if dense is not None:
            idx["vectors"] = dense
        self.index_path(pid).write_text(json.dumps(idx), encoding="utf-8")
        return result(self.name, "builtin+dense" if idx["dense"] else "builtin",
                      passages=len(passages), sources=len(sources), dense=idx["dense"], device=idx["device"])

    def _embed(self, texts: list[str], device: str):
        try:
            from sentence_transformers import SentenceTransformer
            from .. import config
            mdir = config.VENDOR_DIR / "models" / "embeddings"
            if not mdir.exists():
                return None
            dev = "cuda" if device.startswith("cuda") else "cpu"
            model = SentenceTransformer(str(mdir), device=dev)
            return model.encode(texts, normalize_embeddings=True).tolist()
        except Exception:  # noqa: BLE001
            return None

    def verify(self, pid: int, text: str, device: str = "cpu", threshold: float = 0.45) -> dict:
        p = self.index_path(pid)
        if not p.exists():
            return {"tool": self.name, "engine": "builtin", "ok": False, "error": "Index sources first (Step 3)."}
        idx = json.loads(p.read_text(encoding="utf-8"))
        passages = idx["passages"]
        bm = BM25([x["text"] for x in passages]) if passages else None
        claims = []
        for s in split_sentences(text):
            plain = strip_markup(s["text"]).strip()
            cw = set(content_words(plain))
            if len(cw) < 4 or plain.endswith("?"):
                continue
            best, best_score, overlap = None, 0.0, 0.0
            if bm:
                scores = bm.scores(plain)
                order = sorted(range(len(scores)), key=lambda i: -scores[i])[:5]
                for i in order:
                    pw = set(content_words(passages[i]["text"]))
                    ov = len(cw & pw) / len(cw)
                    combined = 0.6 * ov + 0.4 * min(1.0, scores[i] / 12)
                    if combined > best_score:
                        best, best_score, overlap = passages[i], combined, ov
            nums = set(re.findall(r"\d+(?:\.\d+)?", plain))
            num_ok = (not nums) or (best is not None and nums <= set(re.findall(r"\d+(?:\.\d+)?", best["text"])))
            status = "supported" if best_score >= threshold and num_ok else \
                "partial" if best_score >= threshold * 0.6 else "unsupported"
            if nums and not num_ok and status == "supported":
                status = "partial"
            claims.append({"claim": s["text"], "start": s["start"], "end": s["end"], "status": status,
                           "score": round(best_score, 3), "term_overlap": round(overlap, 3),
                           "numbers_match": num_ok,
                           "evidence": {"source": best["source"], "section": best["section"],
                                        "passage": best["text"][:600]} if best else None})
        summary = {k: sum(1 for c in claims if c["status"] == k) for k in ("supported", "partial", "unsupported")}
        return result(self.name, "builtin", claims=claims, summary=summary,
                      note=None if passages else "Index is empty — add source PDFs.")
