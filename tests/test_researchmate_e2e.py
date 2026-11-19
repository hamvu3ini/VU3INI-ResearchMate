"""End-to-end + unit tests for VU3INI ResearchMate.

Run:  python -m pytest tests/test_researchmate_e2e.py -v
All tests are offline: network is forced off, the local LLM is replaced by a fake HTTP endpoint and hardware
profiles are mocked (RTX 5060 8 GB, 4 GB low-VRAM, mismatched dual GPU, integrated-only).
"""
from __future__ import annotations

import io
import json
import re
import threading
import zipfile
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"
PROFILES = json.loads((FIX / "hardware_profiles.json").read_text())


# ======================================================================= fixtures
@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHMATE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("RESEARCHMATE_FORCE_OFFLINE", "1")
    monkeypatch.setenv("RESEARCHMATE_MOCK_HW", json.dumps(PROFILES["rtx5060_8gb"]))
    return tmp_path


class FakeResp:
    def __init__(self, status: int, payload: dict | None = None, text: str = ""):
        self.status_code, self._p, self.text = status, payload or {}, text or json.dumps(payload or {})

    def json(self):
        return self._p


class FakeLLM:
    """Stands in for an Ollama server. Optionally fails the first N calls with a CUDA OOM error."""

    def __init__(self, oom_first: int = 0):
        self.calls: list[dict] = []
        self.oom_left = oom_first

    def get(self, url, *a, **k):
        if url.endswith("/api/tags"):
            return FakeResp(200, {"models": [{"name": "mock-llama:8b"}, {"name": "xtragpt-7b"}]})
        if url.endswith("/api/version"):
            return FakeResp(200, {"version": "0.0-test"})
        raise httpx.ConnectError("refused")

    def post(self, url, json=None, **k):
        self.calls.append(json)
        if self.oom_left:
            self.oom_left -= 1
            return FakeResp(500, text="CUDA error: out of memory")
        prompt = json["prompt"]
        if "<SELECTED_CONTENT>" in prompt:  # XtraGPT revision
            sel = re.search(r"<SELECTED_CONTENT>\n(.*?)\n</SELECTED_CONTENT>", prompt, re.S).group(1)
            return FakeResp(200, {"response": sel.replace("In order to", "To").replace("very large", "large")})
        return FakeResp(200, {"response": (
            "In order to measure cooling, we compared neighbourhoods with more than 30% tree canopy cover and "
            "neighbourhoods with less than 10% canopy [@smith2020]. Neighbourhoods with more than 30% tree canopy cover "
            "were on average 2.4 degrees cooler than neighbourhoods with less than 10% canopy. "
            "Quantum entanglement of municipal budgets determines the colour of lamp posts.")})


@pytest.fixture()
def fake_llm(monkeypatch):
    fake = FakeLLM()
    import backend.adapters.llm as llm
    monkeypatch.setattr(llm.httpx, "get", fake.get)
    monkeypatch.setattr(llm.httpx, "post", fake.post)
    return fake


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient
    from backend.main import create_app
    with TestClient(create_app()) as c:
        yield c


def new_project(client, title="Tree canopy and urban heat"):
    p = client.post("/api/projects", json={"title": title, "sections": ["Abstract", "Results"]}).json()
    client.put(f"/api/projects/{p['id']}/outline", json={
        "question": "Does tree canopy reduce urban heat?", "hypothesis": "More canopy means cooler streets.",
        "outline": "1. Background\n2. Data\n3. Findings", "target_csl": "apa"})
    client.post(f"/api/projects/{p['id']}/bib/import", json={"bib": (FIX / "refs.bib").read_text()})
    return client.get(f"/api/projects/{p['id']}").json()


# ======================================================================= branding / static / DOM
def test_index_branding_and_dom(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "<title>VU3INI ResearchMate</title>" in html
    assert 'id="brand-logo"' in html and 'src="/assets/logo.png"' in html
    # the logo is the first element in the header (top-left corner)
    assert html.index('id="brand-logo"') < html.index('id="project-select"') < html.index('id="hw-pill"')
    assert 'id="version-badge">v1.0.0<' in html or re.search(r'id="version-badge">v\d+\.\d+\.\d+<', html)
    assert '<footer class="credit" id="credit">By Sasanka Boruah -VU3INI</footer>' in html
    assert 'id="net-pill"' in html and 'id="theme-btn"' in html and 'id="help-btn"' in html
    assert "Sasanka Boruah - VU3INI" in html and "hamvu3ini@gmail.com" in html
    # zero CDN dependencies
    assert not re.search(r'(src|href)="https?://', html)


def test_static_assets_served(client):
    for path, ctype in (("/assets/logo.png", "image/png"), ("/assets/favicon.png", "image/png"),
                        ("/assets/logo-fallback.svg", "image/svg"), ("/static/app.js", "javascript"),
                        ("/static/styles.css", "text/css")):
        r = client.get(path)
        assert r.status_code == 200, path
        assert ctype in r.headers["content-type"], path
    css = client.get("/static/styles.css").text
    for colour in ("#EBF3FA", "#D6E6F5", "#2B6CB0", "#FFFFFF", "#F4F6F9", "#2D3748",
                   "#0F172A", "#1E293B", "#334155", "#38BDF8", "#F1F5F9"):
        assert colour in css
    assert ".credit" in css and "position: fixed" in css


def test_health_about_settings_theme_persist(env):
    from fastapi.testclient import TestClient
    from backend.main import create_app
    with TestClient(create_app()) as c:
        assert c.get("/api/health").json()["status"] == "ok"
        a = c.get("/api/about").json()
        assert a["creator"] == "Sasanka Boruah - VU3INI" and a["email"] == "hamvu3ini@gmail.com"
        assert a["hardware"]["gpus"][0]["name"] == "NVIDIA GeForce RTX 5060"
        assert c.put("/api/settings", json={"theme": "dark"}).json()["theme"] == "dark"
        assert c.put("/api/settings", json={"theme": "purple"}).status_code == 400
        pid = c.post("/api/projects", json={"title": "Persisted"}).json()["id"]
    with TestClient(create_app()) as c2:  # fresh app, same SQLite file
        assert c2.get("/api/settings").json()["theme"] == "dark"
        assert c2.get(f"/api/projects/{pid}").json()["title"] == "Persisted"


def test_network_offline_status(client):
    assert client.get("/api/network").json() == {"online": False}


def test_tool_statuses_cover_all_adapters(client):
    tools = client.get("/api/tools").json()["tools"]
    names = {t["name"] for t in tools}
    for n in ("Harper", "LanguageTool", "Vale", "Turnitout-Humanizer", "XtraGPT", "PaperDebugger", "GROBID",
              "PaperQA2", "RefChecker", "CiteScan", "check-citations", "Paper Analysis Utility",
              "Zotero + Better BibTeX", "Typst", "Pandoc + citeproc"):
        assert n in names
    assert len(tools) == 16  # 15 tools + local LLM engine
    assert all(t["engine"] in ("native", "builtin") for t in tools)


def test_workflow_has_21_steps_with_explainers(client):
    wf = client.get("/api/workflow").json()
    assert [s["n"] for s in wf["steps"]] == list(range(1, 22))
    with_opts = {s["n"] for s in wf["steps"] if s.get("options")}
    assert {2, 3, 8, 11, 12, 15, 19} <= with_opts
    assert set(wf["csl_styles"]) == {"apa", "ieee", "vancouver", "chicago-author-date", "nature", "acm-sig-proceedings"}


# ======================================================================= hardware manager
def prof(name):
    from backend.hardware_manager import profile_from_dict
    return profile_from_dict(PROFILES[name])


def test_auto_big_gpu_runs_llm_on_gpu_and_aux_on_cpu():
    from backend.hardware_manager import build_plan, llm_options
    p = build_plan("auto", prof("rtx5060_8gb"))
    assert p["effective"] == "gpu" and p["n_gpu_layers"] == -1
    assert p["embed_device"] == "cpu" and p["cpu_workers"] == 7
    assert p["label"].startswith("Auto → GPU [NVIDIA GeForce RTX 5060]")
    assert llm_options(p)["num_gpu"] == 999


def test_auto_low_vram_activates_hybrid_layer_split():
    from backend.hardware_manager import build_plan, layers_that_fit
    p = build_plan("auto", prof("low_vram_4gb"), model_size_gb=4.9, total_layers=32)
    assert p["effective"] == "hybrid"
    assert p["n_gpu_layers"] == layers_that_fit(3.6, 4.9, 32) == 16
    assert 0 < p["n_gpu_layers"] < 32 and "16/32 layers" in p["label"]


def test_auto_mismatched_multi_gpu_uses_hybrid_with_tensor_split():
    from backend.hardware_manager import build_plan
    p = build_plan("auto", prof("dual_mismatched"), model_size_gb=16, total_layers=48)
    assert p["effective"] == "hybrid"
    assert len(p["tensor_split"]) == 2 and abs(sum(p["tensor_split"]) - 1) < 0.01
    assert p["tensor_split"][0] > p["tensor_split"][1]
    assert p["cuda_visible_devices"] == "0,1"


def test_auto_integrated_only_goes_cpu():
    from backend.hardware_manager import build_plan, plan_env
    p = build_plan("auto", prof("integrated_only"))
    assert p["effective"] == "cpu" and p["n_gpu_layers"] == 0 and p["num_thread"] == 5
    assert plan_env(p)["CUDA_VISIBLE_DEVICES"] == ""
    assert "AVX2" in p["label"]


def test_gpu_only_vram_guard_and_no_gpu_fallback():
    from backend.hardware_manager import build_plan
    p = build_plan("gpu", prof("low_vram_4gb"), model_size_gb=8)
    assert p["effective"] == "gpu" and p["n_gpu_layers"] == -1
    assert any("VRAM guard" in w for w in p["warnings"])
    q = build_plan("gpu", prof("integrated_only"))
    assert q["effective"] == "cpu" and q["warnings"]


def test_cpu_only_hides_gpu():
    from backend.hardware_manager import build_plan, llm_options, plan_env
    p = build_plan("cpu", prof("rtx5060_8gb"))
    assert p["n_gpu_layers"] == 0 and llm_options(p)["num_gpu"] == 0
    assert plan_env(p)["CUDA_VISIBLE_DEVICES"] == ""


def test_hybrid_manual_layers_and_parallel_lanes():
    from backend.hardware_manager import HardwareManager
    hm = HardwareManager(probe=lambda: prof("rtx5060_8gb"))
    plan = hm.set_mode("hybrid", manual_gpu_layers=20)
    assert plan["effective"] == "hybrid" and plan["n_gpu_layers"] == 20
    out = hm.run_parallel({"draft": lambda: threading.current_thread().name},
                          {"lint": lambda: threading.current_thread().name,
                           "index": lambda: threading.current_thread().name})
    assert out["draft"]["result"].startswith("rm-gpu")
    assert out["lint"]["result"].startswith("rm-cpu") and out["index"]["result"].startswith("rm-cpu")
    hm.set_mode("cpu")
    assert hm.submit("gpu", lambda: threading.current_thread().name).result().startswith("rm-cpu")


def test_oom_fallback_halves_gpu_layers_until_success():
    from backend.hardware_manager import build_plan, run_with_oom_fallback
    plan = build_plan("gpu", prof("rtx5060_8gb"))
    seen, events = [], []

    def fn(p):
        seen.append(p["n_gpu_layers"])
        if p["n_gpu_layers"] == -1 or p["n_gpu_layers"] > 8:
            raise RuntimeError("CUDA error: out of memory")
        return "ok"
    assert run_with_oom_fallback(fn, plan, events) == "ok"
    assert seen == [-1, 16, 8] and len(events) == 2 and events[0]["from_layers"] == "all"
    with pytest.raises(ValueError):
        run_with_oom_fallback(lambda p: (_ for _ in ()).throw(ValueError("not oom")), plan)


def test_hardware_api_switch_modes(client):
    snap = client.get("/api/hardware/profile").json()
    assert snap["mode"] == "auto" and set(snap["modes"]) == {"auto", "gpu", "cpu", "hybrid"}
    for mode in ("cpu", "gpu", "hybrid", "auto"):
        r = client.post("/api/hardware/mode", json={"mode": mode}).json()
        assert r["mode"] == mode
    assert client.post("/api/hardware/mode", json={"mode": "turbo"}).status_code == 400
    t = client.get("/api/hardware/telemetry").json()
    assert "cpu_percent" in t and t["gpus"][0]["vram_total_gb"] == 8.0
    assert client.get("/api/settings").json()["compute_mode"] == "auto"


# ======================================================================= individual adapters
def test_harper_and_languagetool_builtin(env):
    from backend.adapters.harper import HarperAdapter
    from backend.adapters.languagetool import LanguageToolAdapter
    text = "This is is a example of teh results. less participants were recruited."
    h = HarperAdapter().check(text)
    rules = {i["rule"] for i in h["issues"]}
    assert {"REPEATED_WORD", "A_VS_AN", "SPELLING"} <= rules
    lt = LanguageToolAdapter().check("We recruited less participants than expected and the data is noisy.")
    assert any("fewer" in i["replacements"] for i in lt["issues"])
    assert lt["engine"] in ("native", "builtin")


def test_vale_house_styles_and_ai_cliches(env):
    from backend.adapters.vale import ValeAdapter, ensure_vale_styles
    root = ensure_vale_styles()
    assert (root / "styles" / "ResearchMate" / "AICliches.yml").exists()
    assert (root / ".vale-IEEE.ini").exists()
    r = ValeAdapter().lint("We delve into the tapestry of data in order to unlock the potential, e.g. here.", "IEEE")
    rules = {i["rule"] for i in r["issues"]}
    assert "ResearchMate.AICliches" in rules and "ResearchMate.Wordiness" in rules and "IEEE.Swap" in rules


def test_humanizer_flags_uniform_cadence(env):
    from backend.adapters.humanizer import HumanizerAdapter, readability
    robotic = " ".join(["The model processes the input data with care."] * 3 +
                       ["The system returns the final answer very fast.", "The user reads the output on the screen.",
                        "The team reviews the results every single week."])
    varied = ("Heat kills. In 2023 alone, extreme temperatures were linked to thousands of excess deaths across "
              "South Asian cities, most of them among outdoor workers and the elderly. Why? Concrete stores heat, "
              "trees do not. We measured it.")
    a, b = HumanizerAdapter().analyze(robotic), HumanizerAdapter().analyze(varied)
    assert any(f["kind"] == "uniform_run" for f in a["flags"])
    assert a["score"] > b["score"] and b["burstiness"] > a["burstiness"]
    assert "flesch" in readability(varied)


def test_xtragpt_and_paperdebugger_builtin_fallback(env, monkeypatch):
    import backend.adapters.llm as llm
    monkeypatch.setattr(llm.LocalLLMAdapter, "backend", lambda self: None)
    from backend.adapters import Registry
    from backend.hardware_manager import build_plan
    reg = Registry()
    plan = build_plan("cpu", prof("rtx5060_8gb"))
    x = reg.xtragpt.revise("In order to test, we utilize a very large dataset due to the fact that it exists.", "", plan)
    assert x["engine"] == "builtin" and "To test" in x["revised"] and "because" in x["revised"]
    d = reg.paperdebugger.revise("Our method proves the theory.\n\nThe data was collected.", "Methods", plan)
    assert d["engine"] == "builtin" and "suggests" in d["revised"]
    assert any("statistical test" in n for n in d["notes"]) and any("transition" in n for n in d["notes"])


def test_grobid_fallback_parses_pdf_and_text(env, tmp_path):
    import fitz
    from backend.adapters.grobid import GrobidAdapter
    pdf = tmp_path / "paper.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Introduction", fontsize=18)
    page.insert_text((72, 110), "Trees cool streets by shading surfaces.", fontsize=10)
    page.insert_text((72, 150), "Results", fontsize=18)
    page.insert_text((72, 190), "Canopy lowered temperature by 2.4 degrees.", fontsize=10)
    doc.save(pdf)
    r = GrobidAdapter().parse(str(pdf))
    assert r["ok"] and r["engine"] == "builtin" and "GROBID service not running" in r["note"]
    heads = [s["head"] for s in r["sections"]]
    assert "Introduction" in heads and "Results" in heads
    t = GrobidAdapter().parse(str(FIX / "source_heat.txt"))
    assert [s["head"] for s in t["sections"]] == ["Abstract", "Results", "Discussion"]


def test_grobid_tei_parser():
    from backend.adapters.grobid import GrobidAdapter
    tei = ('<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><titleStmt><title>T</title></titleStmt>'
           '</fileDesc><profileDesc><abstract><p>Abs text.</p></abstract></profileDesc></teiHeader><text><body>'
           '<div><head>Methods</head><p>We did X.</p></div></body><back><listBibl><biblStruct><analytic><title>Ref A'
           '</title></analytic><idno type="DOI">10.1/x</idno></biblStruct></listBibl></back></text></TEI>')
    secs, refs, title = GrobidAdapter.parse_tei(tei)
    assert title == "T" and secs[0]["head"] == "Abstract" and secs[1]["text"] == "We did X."
    assert refs == [{"title": "Ref A", "doi": "10.1/x"}]


def test_refchecker_offline_and_mocked_online(env, monkeypatch):
    from backend.adapters import citations as c
    bib = (FIX / "refs.bib").read_text()
    off = c.RefCheckerAdapter().check(bib)
    assert not off["online"] and {r["status"] for r in off["references"]} == {"unverified-offline"}

    def handler(req: httpx.Request):
        url = str(req.url)
        if "crossref.org/works/10.1000" in url:
            return httpx.Response(200, json={"message": {"title": ["Urban Heat Islands and Tree Canopy Cover"],
                                                         "DOI": "10.1000/juc.2020.0012", "issued": {"date-parts": [[2020]]},
                                                         "author": [{"given": "John", "family": "Smith"}],
                                                         "container-title": ["Journal of Urban Climate"]}})
        if "crossref.org/works" in url:
            return httpx.Response(200, json={"message": {"items": []}})
        if "openalex" in url:
            return httpx.Response(200, json={"results": []})
        if "semanticscholar" in url:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"></feed>')
    monkeypatch.setenv("RESEARCHMATE_FORCE_OFFLINE", "0")
    monkeypatch.setattr(c, "is_online", lambda: True)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as cl:
        on = c.RefCheckerAdapter().check(bib, client=cl)
    status = {r["key"]: r["status"] for r in on["references"]}
    assert status["smith2020"] == "verified" and status["lee2021"] == "not-found"


def test_citescan_parity_and_clean(env):
    from backend.adapters.citations import CiteScanAdapter
    ms = "Trees cool cities [@smith2020; @lee2021]. Also \\cite{ghost2022}. Email me at a@b.com."
    r = CiteScanAdapter().scan(ms, (FIX / "refs.bib").read_text())
    assert r["missing_in_bib"] == ["ghost2022"] and r["unused_in_bib"] == ["unused2019"] and not r["parity"]
    cleaned = CiteScanAdapter.clean(ms, (FIX / "refs.bib").read_text())
    assert cleaned["removed"] == ["unused2019"] and "unused2019" not in cleaned["bib"]


def test_check_citations_static_validation(env):
    from backend.adapters.citations import CheckCitationsAdapter
    r = CheckCitationsAdapter().validate((FIX / "bad.bib").read_text())
    msgs = " | ".join(p["message"] for p in r["problems"])
    assert not r["valid"]
    for needle in ("Duplicate key", "missing required field: journal", "crossref", "Malformed DOI", "not 4 digits"):
        assert needle in msgs
    assert CheckCitationsAdapter().validate((FIX / "refs.bib").read_text())["valid"]


def test_paper_analysis_abstract_vs_results(env):
    from backend.adapters.citations import PaperAnalysisAdapter
    secs = [{"name": "Abstract", "content": "Canopy cooled streets by 3.1 degrees across 48 neighbourhoods using LST."},
            {"name": "Results", "content": "Land Surface Temperature (LST) fell by 2.4 degrees in 48 neighbourhoods "
                                           "(Figure 1, Figure 3).\n**Figure 1:** Map."}]
    r = PaperAnalysisAdapter().audit(secs)
    msgs = " | ".join(i["message"] for i in r["issues"])
    assert not r["consistent"] and "3.1" in msgs and "48" not in msgs
    assert "LST is used before it is defined" in msgs and "Figure 3 is referenced but has no caption" in msgs


def test_overlap_detection(env):
    from backend.adapters.citations import source_overlap
    src = [{"filename": "heat.txt", "text": (FIX / "source_heat.txt").read_text()}]
    ms = ("Planting street trees is a cost-effective strategy for mitigating urban heat islands in tropical and "
          "temperate cities alike. Our own idea is completely different and new in every possible respect here.")
    r = source_overlap(ms, src)
    assert len(r["flagged"]) == 1 and r["flagged"][0]["kind"] == "verbatim"
    quoted = '"Planting street trees is a cost-effective strategy for mitigating urban heat islands" [@smith2020].'
    assert source_overlap(quoted, src)["flagged"] == []


def test_zotero_unavailable_and_bibtex_parser(env):
    from backend.adapters.bibtex import cited_keys, parse_bib
    from backend.adapters.zotero import ZoteroAdapter
    r = ZoteroAdapter().export("Thesis")
    assert not r["ok"] and "Zotero is not running" in r["error"]
    p = parse_bib('@article{a1, title = {Nested {Braces} ok}, year = 2020, note = "quoted"}\n@article{broken, title={x}\n')
    assert p["entries"][0]["fields"]["title"] == "Nested {Braces} ok" and p["entries"][0]["fields"]["year"] == "2020"
    assert p["errors"]
    assert cited_keys("See [@a; @b, p. 4] and @c, not me@mail.com, and \\citep[see][]{d,e}.") == ["a", "b", "c", "d", "e"]


def test_typst_preview_fallback_and_csl_formats(env):
    from backend.adapters.render import apply_citations
    from backend.adapters.typst_pandoc import TypstAdapter
    bib = (FIX / "refs.bib").read_text()
    r = TypstAdapter().preview("# T\n\n## Intro\n\nTrees help [@smith2020].", bib, "apa", "T")
    assert r["ok"] and (r["format"] == "svg" or "(Smith &amp; Doe, 2020)" in r["html"] or "(Smith & Doe, 2020)" in r["html"])
    body, refs = apply_citations("A [@smith2020; @lee2021]. B [@lee2021].", bib, "ieee")
    assert "[1,2]" in body and "[2]" in body and refs[0].startswith("[1] J. Smith and J. Doe")
    body2, refs2 = apply_citations("A [@lee2021].", bib, "apa")
    assert "(Lee et al., 2021)" in body2 and refs2[0].startswith("Lee, M., Park, S., & Kim, H. (2021).")


def test_pandoc_export_all_formats_and_versioning(env, tmp_path):
    from backend.adapters.typst_pandoc import PandocAdapter
    out = tmp_path / "exports"
    md = "# Paper\n\n## Results\n\nTrees cool streets [@smith2020] by **2.4** degrees.\n"
    bib = (FIX / "refs.bib").read_text()
    res = {f: PandocAdapter().export(md, bib, "vancouver", "Paper", f, out) for f in ("docx", "tex", "pdf")}
    assert all(r["ok"] for r in res.values()), res
    assert {r["version"] for r in res.values()} == {"1.0.0", "1.0.1", "1.0.2"} or \
        res["docx"]["filename"] == "Manuscript_v1.0.0.docx"
    with zipfile.ZipFile(res["docx"]["path"]) as z:
        xml = z.read("word/document.xml").decode()
        assert "Trees cool streets" in xml
        if res["docx"]["engine"] == "builtin":  # built-in citeproc: Vancouver numeric citations + reference list
            assert "(1)" in xml and "References" in xml
        else:  # real pandoc + citeproc
            assert "Smith" in xml
    assert "\\begin{document}" in Path(res["tex"]["path"]).read_text()
    assert Path(res["pdf"]["path"]).read_bytes()[:4] == b"%PDF"
    with pytest.raises(ValueError):
        PandocAdapter().export(md, bib, "apa", "Paper", "odt", out)


# ======================================================================= full 21-step workflow through the API
def test_full_21_step_workflow(client, fake_llm):
    fake_llm.oom_left = 1  # first GPU attempt OOMs → automatic layer halving
    p = new_project(client)
    pid = p["id"]
    abstract, results = p["sections"]
    assert 1 in p["steps_done"] and 2 in p["steps_done"]

    # Step 2: sources upload — PDF/TXT accepted, other types rejected
    up = client.post(f"/api/projects/{pid}/sources",
                     files=[("files", ("source_heat.txt", (FIX / "source_heat.txt").read_bytes(), "text/plain"))])
    assert up.status_code == 200 and len(up.json()["sources"]) == 1
    assert client.post(f"/api/projects/{pid}/sources", files=[("files", ("x.exe", b"MZ", "application/x"))]).status_code == 400

    # Step 3: parse (GROBID fallback) + index (PaperQA2)
    idx = client.post(f"/api/projects/{pid}/index").json()
    assert idx["index"]["passages"] > 0 and idx["parsed"][0]["sections"] == 3

    # Global steps are locked before the matrix is complete
    assert client.post(f"/api/projects/{pid}/grammar", json={"engine": "harper"}).status_code == 409

    # Step 4: draft Results with the (fake) local LLM — OOM retry recorded, AI log written
    d = client.post(f"/api/sections/{results['id']}/draft", json={"instructions": "Report the cooling effect"})
    assert d.status_code == 200, d.text
    dj = d.json()
    assert dj["model"] == "mock-llama:8b" and len(dj["oom_events"]) == 1
    assert fake_llm.calls[0]["options"]["num_gpu"] == 999 and fake_llm.calls[1]["options"]["num_gpu"] == 16
    log = client.get(f"/api/projects/{pid}/ailog").json()["log"]
    assert log[0]["step"] == 4 and log[0]["hardware_mode"].startswith("Auto")

    # Step 5: human edit → edit ratio > 0
    content = dj["section"]["content"].replace("In order to measure cooling, we", "We")
    e = client.put(f"/api/sections/{results['id']}", json={"content": content}).json()
    assert e["status"]["edited"] and e["human_edit_ratio"] > 0

    # Step 6: grounding — the quantum sentence is unsupported, the canopy numbers are supported
    g = client.post(f"/api/sections/{results['id']}/ground").json()
    by = {c["claim"][:20]: c["status"] for c in g["claims"]}
    assert by["Quantum entanglement"] == "unsupported"
    assert any(s == "supported" for s in by.values())

    # Step 7: remove the unsupported claim, re-ground → no unsupported
    q = client.get(f"/api/sections/{results['id']}/claims").json()["queue"]
    bad = next(c for c in q if c["claim"].startswith("Quantum"))
    client.post(f"/api/sections/{results['id']}/claims/remove", json={"claim": bad["claim"]})
    g2 = client.post(f"/api/sections/{results['id']}/ground").json()
    assert g2["summary"]["unsupported"] == 0

    # Step 8: XtraGPT revision (native path via fake LLM) with CPU-lane parallel linting
    client.put(f"/api/sections/{results['id']}", json={"content": "In order to compare, we used a very large sample. "
               + client.get(f"/api/sections/{results['id']}").json()["content"]})
    client.post(f"/api/sections/{results['id']}/ground")
    rv = client.post(f"/api/sections/{results['id']}/revise", json={"engine": "xtragpt"}).json()
    assert rv["engine"] == "native" and rv["model"] == "xtragpt-7b"
    assert rv["parallel"]["harper"]["ok"] and rv["parallel"]["vale"]["ok"]
    hunks = rv["revision"]["hunks"]
    assert len(hunks) >= 2

    # Step 9: accept the first hunk, reject the rest
    decisions = {str(h["id"]): ("accept" if h["id"] == 0 else "reject") for h in hunks}
    ap = client.post(f"/api/revisions/{rv['revision']['id']}/apply", json={"decisions": decisions}).json()
    assert ap["accepted"] == 1 and ap["rejected"] == len(hunks) - 1
    txt = ap["section"]["content"]
    assert txt.startswith("To compare") and "very large" in txt
    assert client.post(f"/api/revisions/{rv['revision']['id']}/apply", json={"decisions": {}}).status_code == 409

    # Abstract written by hand (no AI) and grounded
    client.put(f"/api/sections/{abstract['id']}", json={
        "content": "Neighbourhoods with more than 30% tree canopy cover were on average 2.4 degrees cooler [@smith2020]."})
    client.post(f"/api/sections/{abstract['id']}/ground")
    client.post(f"/api/sections/{results['id']}/ground")

    # Step 10: completion matrix unlocks global checks
    m = client.get(f"/api/projects/{pid}/matrix").json()
    assert m["complete"], m

    # Step 11–13
    for eng in ("harper", "languagetool"):
        gr = client.post(f"/api/projects/{pid}/grammar", json={"engine": eng}).json()
        assert len(gr["sections"]) == 2
    st = client.post(f"/api/projects/{pid}/style", json={"engine": "both", "house_style": "Nature"}).json()
    assert st["vale"] and st["humanizer"]
    an = client.post("/api/analyze", json={"text": txt}).json()
    assert "readability" in an and "score" in an["humanizer"]

    # Step 14 (offline → graceful), Step 15 parity + clean
    rc = client.post(f"/api/projects/{pid}/refcheck").json()
    assert rc["online"] is False and rc["summary"] == {"unverified-offline": 3}
    ct = client.post(f"/api/projects/{pid}/citations").json()
    assert ct["citescan"]["unused_in_bib"] == ["lee2021", "unused2019"]
    cl = client.post(f"/api/projects/{pid}/bib/clean").json()
    assert cl["kept"] == ["smith2020"]
    ct2 = client.post(f"/api/projects/{pid}/citations").json()
    assert ct2["citescan"]["parity"] and ct2["check_citations"]["valid"]

    # Step 16–17
    ov = client.post(f"/api/projects/{pid}/overlap").json()
    assert ov["sentences_checked"] > 0 and all(f["source"] == "source_heat.txt" for f in ov["flagged"])
    cs = client.post(f"/api/projects/{pid}/consistency").json()
    assert cs["consistent"], cs

    # Step 18: disclosure from Step 4 + Step 8 logs
    disc = client.get(f"/api/projects/{pid}/disclosure").json()
    assert "mock-llama:8b" in disc["statement"] and "xtragpt-7b" in disc["statement"]
    assert disc["sections_drafted"] == ["Results"] and disc["sections_revised"] == ["Results"]
    assert {r["step"] for r in disc["log"]} == {4, 8}

    # Step 19: preview + versioned exports (1.0.0 then 1.0.1)
    pv = client.post(f"/api/projects/{pid}/preview", json={"csl": "ieee"}).json()
    assert pv["ok"]
    ex1 = client.post(f"/api/projects/{pid}/export", json={"formats": ["docx", "tex", "pdf"], "csl": "ieee"}).json()
    assert ex1["version"] == "1.0.0" and all(r["ok"] for r in ex1["results"])
    ex2 = client.post(f"/api/projects/{pid}/export", json={"format": "docx", "csl": "apa"}).json()
    assert ex2["version"] == "1.0.1"
    files = client.get(f"/api/projects/{pid}/exports").json()["files"]
    assert {"Manuscript_v1.0.0.docx", "Manuscript_v1.0.0.tex", "Manuscript_v1.0.0.pdf", "Manuscript_v1.0.1.docx"} <= set(files)
    dl = client.get(f"/api/files/{pid}/Manuscript_v1.0.0.docx")
    assert dl.status_code == 200 and dl.content[:2] == b"PK"
    assert client.get(f"/api/files/{pid}/..%2F..%2Fresearchmate.sqlite3").status_code == 404
    assert client.post(f"/api/projects/{pid}/export", json={"format": "docx", "csl": "mla"}).status_code == 400

    # Step 20–21
    client.post(f"/api/projects/{pid}/steps/20", json={"done": True})
    client.post(f"/api/projects/{pid}/figures", files=[("files", ("fig1.png", b"\x89PNG\r\n", "image/png"))])
    sb = client.post(f"/api/projects/{pid}/submit", json={"checked": [0, 1]}).json()
    assert sb["bundle"] == "Submission_Bundle_v1.0.1.zip"
    assert {"Manuscript_v1.0.1.docx", "references.bib", "AI_Use_Disclosure.md", "figures/fig1.png",
            "Pre-submission_Checklist.md"} <= set(sb["contents"])
    z = zipfile.ZipFile(io.BytesIO(client.get(sb["url"]).content))
    assert "unused2019" not in z.read("references.bib").decode()
    assert "- [x]" in z.read("Pre-submission_Checklist.md").decode()

    done = set(client.get(f"/api/projects/{pid}").json()["steps_done"])
    assert {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 17, 18, 19, 20, 21} <= done


def test_step1_required_before_drafting_and_llm_unavailable(client, monkeypatch):
    p = client.post("/api/projects", json={"title": "No outline yet"}).json()
    sid = p["sections"][0]["id"]
    r = client.post(f"/api/sections/{sid}/draft", json={})
    assert r.status_code == 409 and "Step 1" in r.json()["detail"]
    client.put(f"/api/projects/{p['id']}/outline", json={"question": "Q?", "outline": "1. A"})
    import backend.adapters.llm as llm
    monkeypatch.setattr(llm.LocalLLMAdapter, "backend", lambda self: None)
    r2 = client.post(f"/api/sections/{sid}/draft", json={})
    assert r2.status_code == 503 and "Ollama" in r2.json()["detail"]
    assert client.post(f"/api/projects/{p['id']}/bib/zotero", json={}).status_code == 503
    assert client.post(f"/api/projects/{p['id']}/index").status_code == 400  # no sources yet
    assert client.get("/api/projects/9999").status_code == 404


def test_hunk_level_diff_roundtrip():
    from backend.adapters.diffing import apply_decisions, make_hunks
    a = "The cat sat on the mat. It was very happy indeed."
    b = "The dog sat on the rug. It was happy."
    hunks = make_hunks(a, b)
    assert len(hunks) >= 3
    assert apply_decisions(a, b, {h["id"]: "accept" for h in hunks}) == b
    assert apply_decisions(a, b, {}) == a
    partial = apply_decisions(a, b, {0: "accept"})
    assert partial.startswith("The dog sat on the mat.")
    for h in hunks:
        assert a[h["a_start"]:h["a_end"]] == h["before"]


def test_gating_can_be_relaxed_in_settings(client):
    p = new_project(client, "Relaxed")
    client.put("/api/settings", json={"strict_gating": False})
    assert client.post(f"/api/projects/{p['id']}/consistency").status_code == 200
    client.put("/api/settings", json={"strict_gating": True})


def test_builtin_exporter_when_no_pandoc(env, tmp_path, monkeypatch):
    """Simulate a PC where pandoc/typst are not vendored: built-in citeproc + DOCX/LaTeX/PDF writers take over."""
    from backend import config
    from backend.adapters.typst_pandoc import PandocAdapter, TypstAdapter
    monkeypatch.setattr(config, "find_binary", lambda *names: None)
    md = "# Paper\n\n## Results\n\nTrees cool streets [@smith2020] by **2.4** degrees.\n"
    bib = (FIX / "refs.bib").read_text()
    out = tmp_path / "ex"
    d = PandocAdapter().export(md, bib, "vancouver", "Paper", "docx", out)
    t = PandocAdapter().export(md, bib, "ieee", "Paper", "tex", out, version=d["version"])
    pdf = PandocAdapter().export(md, bib, "apa", "Paper", "pdf", out, version=d["version"])
    assert d["engine"] == t["engine"] == pdf["engine"] == "builtin" and "pandoc.exe not found" in d["note"]
    xml = zipfile.ZipFile(d["path"]).read("word/document.xml").decode()
    assert "Trees cool streets" in xml and "(1)" in xml and "References" in xml and 'w:val="Title"' in xml
    tex = Path(t["path"]).read_text()
    assert "\\begin{document}" in tex and "[1]" in tex and "\\textbf{2.4}" in tex
    assert Path(pdf["path"]).read_bytes()[:4] == b"%PDF"
    assert sorted(p.name for p in out.iterdir()) == ["Manuscript_v1.0.0.docx", "Manuscript_v1.0.0.pdf", "Manuscript_v1.0.0.tex"]
    pv = TypstAdapter().preview(md, bib, "apa", "Paper")
    assert pv["engine"] == "builtin" and "(Smith &amp; Doe, 2020)" in pv["html"]


# ======================================================================= Step 4 prerequisite: Ollama auto-setup
class FakeOllamaPC:
    """Simulates a Windows PC: Ollama may be missing, stopped, and without llama3.1:8b."""

    def __init__(self, installed=False, running=False, models=(), online=True, installer=True, gguf=False):
        self.installed, self.running, self.models_ = installed, running, list(models)
        self.online, self.installer, self.gguf = online, installer, gguf
        self.calls: list[str] = []

    def patch(self, mp, setup_mod, obj):
        mp.setattr(setup_mod, "is_online", lambda: self.online)
        mp.setattr(setup_mod.time, "sleep", lambda s: None)
        mp.setattr(obj, "find_exe", lambda: r"C:\Users\me\AppData\Local\Programs\Ollama\ollama.exe" if self.installed else None)
        mp.setattr(obj, "is_running", lambda: self.running)
        mp.setattr(obj, "models", lambda: list(self.models_))
        mp.setattr(obj, "vendored_gguf", lambda: Path("vendor/models/ollama/x.gguf") if self.gguf else None)

        def install(progress):
            if not self.installer and not self.online:
                raise RuntimeError("Ollama is not installed, no installer is bundled and the PC is offline.")
            self.calls.append("install"); self.installed = True; progress(1.0, "installed")
            return obj.find_exe()

        def serve(exe):
            self.calls.append("serve"); self.running = True

        def pull(model, progress):
            self.calls.append(f"pull {model}"); progress(0.5, "pulling"); self.models_.append(model)

        def import_gguf(exe, gguf, model, progress):
            self.calls.append(f"import {model}"); self.models_.append(model)
        mp.setattr(obj, "install", install)
        mp.setattr(obj, "start_serve", serve)
        mp.setattr(obj, "pull", pull)
        mp.setattr(obj, "import_gguf", import_gguf)


def test_ollama_setup_fresh_pc_installs_serves_and_pulls(env, monkeypatch):
    import backend.adapters.ollama_setup as m
    s = m.OllamaSetup()
    pc = FakeOllamaPC()
    pc.patch(monkeypatch, m, s)
    st = s.run()
    assert st["job"]["stage"] == "ready", st["job"]
    assert pc.calls == ["install", "serve", "pull llama3.1:8b"]
    assert st["ready"] and st["has_default_model"]


def test_ollama_setup_skips_whatever_is_already_done(env, monkeypatch):
    import backend.adapters.ollama_setup as m
    s = m.OllamaSetup()
    pc = FakeOllamaPC(installed=True, running=True, models=["llama3.1:8b"])
    pc.patch(monkeypatch, m, s)
    assert s.run()["job"]["stage"] == "ready" and pc.calls == []
    pc2 = FakeOllamaPC(installed=True, running=False, models=["llama3.1:latest"])
    s2 = m.OllamaSetup()
    pc2.patch(monkeypatch, m, s2)
    assert s2.run()["job"]["stage"] == "ready" and pc2.calls == ["serve"]   # :latest is the 8b tag


def test_ollama_setup_offline_imports_bundled_gguf_or_fails_clearly(env, monkeypatch):
    import backend.adapters.ollama_setup as m
    s = m.OllamaSetup()
    pc = FakeOllamaPC(installed=True, online=False, gguf=True)
    pc.patch(monkeypatch, m, s)
    assert s.run()["job"]["stage"] == "ready" and pc.calls == ["serve", "import llama3.1:8b"]
    s2 = m.OllamaSetup()
    pc2 = FakeOllamaPC(online=False, installer=False)
    pc2.patch(monkeypatch, m, s2)
    st = s2.run()
    assert st["job"]["stage"] == "error" and "offline" in st["job"]["error"] and st["job"]["done"]


def test_ollama_setup_api_and_default_model(client, monkeypatch):
    import backend.adapters.ollama_setup as m
    s = client.app.state.llm_setup
    pc = FakeOllamaPC()
    pc.patch(monkeypatch, m, s)
    st0 = client.get("/api/llm/setup/status").json()
    assert not st0["installed"] and not st0["ready"] and st0["default_model"] == "llama3.1:8b"
    st = client.post("/api/llm/setup", json={"wait": True}).json()
    assert st["job"]["stage"] == "ready" and pc.calls == ["install", "serve", "pull llama3.1:8b"]
    assert client.get("/api/settings").json()["llm_model"] == "llama3.1:8b"
    # background mode returns immediately and finishes on its own thread
    pc.models_.clear()
    client.post("/api/llm/setup", json={})
    s._thread.join(timeout=5)
    assert client.get("/api/llm/setup/status").json()["job"]["stage"] == "ready"


def test_draft_prefers_llama31_8b(client, fake_llm):
    p = new_project(client, "Default model")
    client.put("/api/settings", json={"llm_model": ""})
    r = client.post(f"/api/sections/{p['sections'][0]['id']}/draft", json={}).json()
    assert r["model"] in ("llama3.1:8b", "mock-llama:8b")
    assert fake_llm.calls[-1]["model"] == r["model"]


# ======================================================================= bundled tools + GPU drafting via llama.cpp CUDA
def test_bundled_tools_are_found_and_put_on_path(env, tmp_path, monkeypatch):
    from backend import config
    tools = tmp_path / "vendor" / "bin" / "tools"
    (tools / "Library" / "bin").mkdir(parents=True)
    (tools / "bin").mkdir()
    exe = ".exe" if __import__("sys").platform.startswith("win") else ""
    for n in ("pandoc", "typst", "harper-cli", "ollama", "llama-server"):
        (tools / "Library" / "bin" / (n + exe)).write_text("x")
    (tools / "bin" / ("vale" + exe)).write_text("x")
    monkeypatch.setattr(config, "VENDOR_BIN", tmp_path / "vendor" / "bin")
    monkeypatch.setattr(config, "CONDA_TOOLS", tools)
    monkeypatch.setattr(config, "_bin_cache", {})
    monkeypatch.setenv("PATH", "/usr/bin")
    config.activate_vendored_tools()
    assert str(tools / "Library" / "bin") in __import__("os").environ["PATH"]
    for n in ("pandoc", "typst", "harper-cli", "ollama", "llama-server", "vale"):
        assert config.find_binary(n).startswith(str(tools)), n


def test_ollama_model_gguf_resolution(tmp_path):
    from backend.adapters.llm import ollama_model_gguf
    root = tmp_path / "models"
    man = root / "manifests" / "registry.ollama.ai" / "library" / "llama3.1" / "8b"
    man.parent.mkdir(parents=True)
    man.write_text(json.dumps({"layers": [
        {"mediaType": "application/vnd.ollama.image.template", "digest": "sha256:aaa"},
        {"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:bbb"}]}))
    (root / "blobs").mkdir()
    (root / "blobs" / "sha256-bbb").write_bytes(b"GGUF")
    assert ollama_model_gguf("llama3.1:8b", root) == root / "blobs" / "sha256-bbb"
    assert ollama_model_gguf("llama3.1:70b", root) is None


def test_gpu_drafting_uses_llamacpp_with_plan_layers_and_oom_restart(env, monkeypatch, tmp_path):
    import backend.adapters.llm as llm
    from backend.hardware_manager import build_plan
    a = llm.LocalLLMAdapter()
    gguf = tmp_path / "sha256-bbb"
    gguf.write_bytes(b"GGUF")
    monkeypatch.setattr(a, "backend", lambda: "ollama")
    monkeypatch.setattr(a, "models", lambda: ["llama3.1:8b"])
    monkeypatch.setattr(llm.config, "find_binary", lambda *n: "C:/tools/llama-server.exe" if "llama-server" in n else None)
    monkeypatch.setattr(llm, "ollama_model_gguf", lambda m, d=None: gguf)
    starts = []

    def ensure(path, plan, ctx=8192):
        starts.append(plan["n_gpu_layers"])
        if plan["n_gpu_layers"] in (-1, 32):
            raise RuntimeError("llama-server exited while loading the model: CUDA error: out of memory")
    monkeypatch.setattr(a, "ensure_llama_server", ensure)
    monkeypatch.setattr(llm.httpx, "post", lambda url, json=None, **k: FakeResp(
        200, {"choices": [{"message": {"content": "GPU draft."}}]}))
    events = []
    out = a.generate("Write it", plan=build_plan("gpu", prof("rtx5060_8gb")), oom_events=events)
    assert out["backend"] == "llamacpp-gpu" and out["text"] == "GPU draft."
    assert starts == [-1, 16] and len(events) == 1
    # CPU Only → never starts the GPU server, goes to Ollama with num_gpu = 0
    sent = {}
    monkeypatch.setattr(llm.httpx, "post", lambda url, json=None, **k: (sent.update(url=url, body=json),
                        FakeResp(200, {"response": "CPU draft."}))[1])
    out2 = a.generate("Write it", plan=build_plan("cpu", prof("rtx5060_8gb")))
    assert out2["backend"] == "ollama" and sent["body"]["options"]["num_gpu"] == 0 and starts == [-1, 16]


def test_setup_uses_bundled_ollama_without_installing(env, monkeypatch):
    import backend.adapters.ollama_setup as m
    s = m.OllamaSetup()
    pc = FakeOllamaPC(installed=True)
    pc.patch(monkeypatch, m, s)
    monkeypatch.setattr(s, "is_bundled", lambda exe: True)
    st = s.run()
    assert st["job"]["stage"] == "ready" and pc.calls == ["serve", "pull llama3.1:8b"]
    assert any("bundled with ResearchMate" in l or "Using the Ollama bundled" in l for l in st["job"]["log"])


# ======================================================================= Step 2 prerequisite: Zotero + Better BibTeX auto-setup
class FakeZoteroPC:
    def __init__(self, installed=False, running=False, bbt=False, close_ok=True, online=True, profile=True):
        self.installed, self.running, self.bbt, self.close_ok = installed, running, bbt, close_ok
        self.online, self.has_profile = online, profile
        self.bbt_loaded = running and bbt
        self.calls: list[str] = []

    def patch(self, mp, mod, obj, tmp):
        prof = tmp / "Profiles" / "abc.default"
        mp.setattr(mod, "is_online", lambda: self.online)
        mp.setattr(mod.time, "sleep", lambda s: None)
        mp.setattr(obj, "find_exe", lambda: r"C:\Program Files\Zotero\zotero.exe" if self.installed else None)
        mp.setattr(obj, "is_running", lambda: self.running)
        mp.setattr(obj, "process_alive", lambda: self.running)
        mp.setattr(obj, "bbt_ready", lambda: self.running and self.bbt_loaded)
        mp.setattr(obj, "bbt_responding", lambda: self.running and self.bbt_loaded)
        mp.setattr(obj, "bbt_installed", lambda: self.bbt)
        mp.setattr(obj, "profile", lambda: prof if (self.has_profile or self.calls) else None)

        def install(progress):
            self.calls.append("install"); self.installed = True
            return obj.find_exe()

        def start(exe):
            self.calls.append("start"); self.running = True; self.bbt_loaded = self.bbt

        def close(timeout=45):
            self.calls.append("close")
            if self.close_ok:
                self.running = False; self.bbt_loaded = False
            return self.close_ok

        def get_xpi(progress):
            self.calls.append("xpi")
            return tmp / "zotero-better-bibtex-7.0.0.xpi"

        def install_xpi(xpi, profile):
            self.calls.append("install_xpi"); self.bbt = True
        mp.setattr(obj, "install_zotero", install)
        mp.setattr(obj, "start_zotero", start)
        mp.setattr(obj, "close_zotero_gracefully", close)
        mp.setattr(obj, "get_xpi", get_xpi)
        mp.setattr(obj, "install_xpi", install_xpi)


def test_zotero_setup_fresh_pc(env, monkeypatch, tmp_path):
    import backend.adapters.zotero_setup as m
    s = m.ZoteroSetup()
    pc = FakeZoteroPC()
    pc.patch(monkeypatch, m, s, tmp_path)
    st = s.run()
    assert st["job"]["stage"] == "ready", st["job"]
    assert pc.calls == ["install", "start", "xpi", "close", "install_xpi", "start"]


def test_zotero_setup_skips_done_steps(env, monkeypatch, tmp_path):
    import backend.adapters.zotero_setup as m
    s = m.ZoteroSetup()
    pc = FakeZoteroPC(installed=True, running=True, bbt=True)
    pc.patch(monkeypatch, m, s, tmp_path)
    assert s.run()["job"]["stage"] == "ready" and pc.calls == []
    s2 = m.ZoteroSetup()
    pc2 = FakeZoteroPC(installed=True, running=False, bbt=True)
    pc2.patch(monkeypatch, m, s2, tmp_path)
    assert s2.run()["job"]["stage"] == "ready" and pc2.calls == ["start"]


def test_zotero_setup_never_forces_close(env, monkeypatch, tmp_path):
    import backend.adapters.zotero_setup as m
    s = m.ZoteroSetup()
    pc = FakeZoteroPC(installed=True, running=True, bbt=False, close_ok=False)
    pc.patch(monkeypatch, m, s, tmp_path)
    st = s.run()
    assert st["job"]["stage"] == "error" and "Close Zotero yourself" in st["job"]["error"]
    assert "install_xpi" not in pc.calls and st["job"]["failed_stage"] == "bbt"
    s2 = m.ZoteroSetup()
    pc2 = FakeZoteroPC(online=False)
    pc2.patch(monkeypatch, m, s2, tmp_path)
    monkeypatch.setattr(s2, "install_zotero", m.ZoteroSetup.install_zotero.__get__(s2))
    monkeypatch.setattr(s2, "vendored_installer", lambda: None)
    monkeypatch.setattr(m.sys, "platform", "win32")
    st2 = s2.run()
    assert st2["job"]["stage"] == "error" and "offline" in st2["job"]["error"]


def test_zotero_profile_and_user_prefs(tmp_path):
    from backend.adapters.zotero_setup import BBT_ID, ZoteroSetup, default_profile, write_user_prefs
    root = tmp_path / "Zotero"
    (root / "Profiles" / "x1.default").mkdir(parents=True)
    (root / "profiles.ini").write_text("[General]\nStartWithLastProfile=1\n\n[Profile0]\nName=default\n"
                                       "IsRelative=1\nPath=Profiles/x1.default\nDefault=1\n")
    prof = default_profile(root)
    assert prof == root / "Profiles" / "x1.default"
    (prof / "user.js").write_text('user_pref("extensions.autoDisableScopes", 15);\nuser_pref("other.pref", 1);\n')
    changed = write_user_prefs(prof)
    txt = (prof / "user.js").read_text()
    assert 'user_pref("extensions.autoDisableScopes", 0);' in txt and 'user_pref("other.pref", 1);' in txt
    assert 'extensions.zotero.httpServer.localAPI.enabled", true' in txt and len(changed) == 3
    assert write_user_prefs(prof) == []                       # idempotent
    xpi = tmp_path / "bbt.xpi"
    xpi.write_bytes(b"PK")
    ZoteroSetup().install_xpi(xpi, prof)
    assert (prof / "extensions" / f"{BBT_ID}.xpi").exists()


def test_zotero_collections_paths_and_api(client, monkeypatch, tmp_path):
    import backend.adapters.zotero as z
    import backend.adapters.zotero_setup as m
    cols = [{"key": "A", "data": {"name": "PhD", "parentCollection": False}},
            {"key": "B", "data": {"name": "Chapter 2", "parentCollection": "A"}},
            {"key": "C", "data": {"name": "Heat", "parentCollection": "B"}}]
    monkeypatch.setattr(z.httpx, "get", lambda url, **k: FakeResp(200, cols) if "/api/users/0/collections" in url
                        else FakeResp(404))
    out = client.get("/api/zotero/collections").json()["collections"]
    assert [c["path"] for c in out] == ["PhD", "PhD/Chapter 2", "PhD/Chapter 2/Heat"]
    s = client.app.state.zotero_setup
    pc = FakeZoteroPC(installed=True)
    pc.patch(monkeypatch, m, s, tmp_path)
    st = client.post("/api/zotero/setup", json={"wait": True}).json()
    assert st["job"]["stage"] == "ready" and st["ready"]
    assert client.get("/api/zotero/setup/status").json()["bbt_ready"]
