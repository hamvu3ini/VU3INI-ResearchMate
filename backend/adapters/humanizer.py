"""Turnitout-Humanizer — stylometric burstiness / sentence-variance analyser.

Flags runs of uniformly sized sentences, repetitive sentence openers and low lexical diversity —
the "robotic cadence" typical of unedited LLM prose — so YOU can rewrite them. Optional neural
perplexity scoring runs on the device chosen by the compute plan (cuda / dml / cpu) when a local
causal LM is vendored under vendor/models/perplexity (e.g. GPT-2)."""
from __future__ import annotations

import math
import statistics
from collections import Counter

from .. import config
from .base import Adapter, result, split_sentences, words


class HumanizerAdapter(Adapter):
    key = "humanizer"
    name = "Turnitout-Humanizer"
    purpose = "Stylometric burstiness and sentence-variance analyser that flags uniform robotic cadence."
    strengths = "Explains *why* a passage reads machine-like (rhythm, openers, vocabulary) so you rewrite it yourself."
    repo = "(set URL in scripts/vendor_manifest.json)"

    def model_dir(self):
        p = config.VENDOR_DIR / "models" / "perplexity"
        return p if p.exists() else None

    def native_available(self) -> bool:
        if not self.model_dir():
            return False
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
            return True
        except ImportError:
            return False

    def native_version(self) -> str:
        return "neural perplexity enabled"

    def analyze(self, text: str, device: str = "cpu") -> dict:
        sents = split_sentences(text)
        lengths = [len(words(s["text"])) for s in sents]
        out: dict = {"sentences": len(sents)}
        if len(lengths) < 2:
            return result(self.name, "builtin", **out, burstiness=0.0, flags=[], score=0, verdict="Too short to analyse",
                          device="cpu")
        mean = statistics.mean(lengths)
        sd = statistics.pstdev(lengths)
        burst = round(sd / mean, 3) if mean else 0.0
        all_words = words(text)
        ttr = round(len(set(all_words)) / len(all_words), 3) if all_words else 0
        flags = []
        # uniform runs: ≥4 consecutive sentences within ±15 % of their run mean
        i = 0
        while i < len(lengths):
            j = i + 1
            while j < len(lengths):
                run = lengths[i:j + 1]
                m = statistics.mean(run)
                if all(abs(x - m) <= max(2, 0.15 * m) for x in run):
                    j += 1
                else:
                    break
            if j - i >= 4:
                flags.append({"kind": "uniform_run", "start": sents[i]["start"], "end": sents[j - 1]["end"],
                              "message": f"{j - i} consecutive sentences of ~{round(statistics.mean(lengths[i:j]))} "
                                         f"words — vary length and rhythm."})
                i = j
            else:
                i += 1
        openers = Counter(words(s["text"])[0] for s in sents if words(s["text"]))
        for w, c in openers.items():
            if c >= 3 and c / len(sents) >= 0.25:
                for s in sents:
                    if words(s["text"])[:1] == [w]:
                        flags.append({"kind": "repeated_opener", "start": s["start"], "end": s["end"],
                                      "message": f"{c} sentences open with “{w}” — vary sentence openings."})
        perplexity = None
        engine = "builtin"
        if self.native_available():
            try:
                perplexity, device = self._perplexity(text, device)
                engine = "native"
            except Exception:  # noqa: BLE001
                perplexity = None
        # 0 (human-like) … 100 (machine-like)
        score = 0.0
        score += max(0.0, (0.45 - burst) / 0.45) * 50
        score += max(0.0, (0.55 - ttr) / 0.55) * 20 if len(all_words) > 80 else 0
        score += min(30.0, 10 * sum(1 for f in flags if f["kind"] == "uniform_run"))
        if perplexity is not None:
            score = 0.7 * score + 0.3 * max(0.0, min(100.0, (60 - perplexity) / 60 * 100))
        score = round(min(100.0, score), 1)
        verdict = ("Natural, varied rhythm" if score < 30 else "Some uniform cadence — review flagged passages"
                   if score < 60 else "Robotic cadence — rewrite flagged passages in your own words")
        return result(self.name, engine, **out, burstiness=burst, mean_sentence_words=round(mean, 1),
                      sd_sentence_words=round(sd, 1), type_token_ratio=ttr, lengths=lengths, flags=flags,
                      perplexity=perplexity, score=score, verdict=verdict, device=device if engine == "native" else "cpu")

    def _perplexity(self, text: str, device: str) -> tuple[float, str]:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        dev = device if device.startswith("cuda") and torch.cuda.is_available() else "cpu"
        tok = AutoTokenizer.from_pretrained(str(self.model_dir()))
        model = AutoModelForCausalLM.from_pretrained(str(self.model_dir())).to(dev).eval()
        ids = tok(text[:4000], return_tensors="pt").input_ids[:, :1024].to(dev)
        with torch.no_grad():
            loss = model(ids, labels=ids).loss.item()
        return round(math.exp(loss), 2), dev


def readability(text: str) -> dict:
    """Flesch reading ease + Flesch-Kincaid grade for the rewrite workbench."""
    sents = max(1, len(split_sentences(text)))
    ws = words(text)
    if not ws:
        return {"flesch": 0, "grade": 0}

    def syl(w: str) -> int:
        w = w.lower().strip("'")
        groups = len([g for g in __import__("re").findall(r"[aeiouy]+", w)])
        if w.endswith("e") and groups > 1:
            groups -= 1
        return max(1, groups)
    syllables = sum(syl(w) for w in ws)
    wps, spw = len(ws) / sents, syllables / len(ws)
    return {"flesch": round(206.835 - 1.015 * wps - 84.6 * spw, 1), "grade": round(0.39 * wps + 11.8 * spw - 15.59, 1)}
