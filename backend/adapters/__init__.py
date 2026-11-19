"""Tool adapter registry — 15 research tools + the local LLM engine."""
from __future__ import annotations

from .citations import CheckCitationsAdapter, CiteScanAdapter, PaperAnalysisAdapter, RefCheckerAdapter
from .grobid import GrobidAdapter
from .harper import HarperAdapter
from .humanizer import HumanizerAdapter
from .languagetool import LanguageToolAdapter
from .llm import LocalLLMAdapter
from .paperqa import PaperQAAdapter
from .revision import PaperDebuggerAdapter, XtraGPTAdapter
from .typst_pandoc import PandocAdapter, TypstAdapter
from .vale import ValeAdapter
from .zotero import ZoteroAdapter


class Registry:
    def __init__(self):
        self.llm = LocalLLMAdapter()
        self.harper = HarperAdapter()
        self.languagetool = LanguageToolAdapter()
        self.vale = ValeAdapter()
        self.humanizer = HumanizerAdapter()
        self.xtragpt = XtraGPTAdapter(self.llm)
        self.paperdebugger = PaperDebuggerAdapter(self.llm)
        self.grobid = GrobidAdapter()
        self.paperqa = PaperQAAdapter()
        self.refchecker = RefCheckerAdapter()
        self.citescan = CiteScanAdapter()
        self.check_citations = CheckCitationsAdapter()
        self.paper_analysis = PaperAnalysisAdapter()
        self.zotero = ZoteroAdapter()
        self.typst = TypstAdapter()
        self.pandoc = PandocAdapter()

    def all(self):
        return [self.llm, self.harper, self.languagetool, self.vale, self.humanizer, self.xtragpt, self.paperdebugger,
                self.grobid, self.paperqa, self.refchecker, self.citescan, self.check_citations, self.paper_analysis,
                self.zotero, self.typst, self.pandoc]

    def statuses(self) -> list[dict]:
        return [a.status() for a in self.all()]
