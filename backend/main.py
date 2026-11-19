"""VU3INI ResearchMate — FastAPI server. The entire UI runs in the user's default browser at 127.0.0.1."""
from __future__ import annotations

import io
import json
import platform
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config
from .adapters import Registry
from .adapters.bibtex import cited_keys, parse_bib
from .adapters.citations import CiteScanAdapter, source_overlap
from .adapters.diffing import apply_decisions, make_hunks
from .adapters.disclosure import build_disclosure
from .adapters.humanizer import readability
from .adapters.llm import DRAFT_SYSTEM, LLMUnavailable, draft_prompt
from .adapters.ollama_setup import DEFAULT_MODEL, OllamaSetup, model_present
from .adapters.zotero_setup import ZoteroSetup
from .adapters.render import CSL_STYLES, assemble_markdown
from .adapters.typst_pandoc import next_version
from .database import Database
from .hardware_manager import MODE_LABELS, HardwareManager
from .netstatus import is_online
from .workflow import CHECKLIST, STEPS


def create_app(hw: HardwareManager | None = None) -> FastAPI:
    data = config.data_dir()
    db = Database(data / "researchmate.sqlite3")
    reg = Registry()
    hw = hw or HardwareManager()
    saved_mode = db.get_setting("compute_mode", "auto")
    try:
        hw.set_mode(saved_mode, model_size_gb=db.get_setting("model_size_gb", 4.9),
                    total_layers=db.get_setting("model_layers", 32),
                    manual_gpu_layers=db.get_setting("manual_gpu_layers", None))
    except ValueError:
        hw.set_mode("auto")

    app = FastAPI(title=config.APP_NAME, version=config.version(), docs_url="/api/docs", redoc_url=None)
    llm_setup = OllamaSetup()
    zotero_setup = ZoteroSetup()
    app.state.zotero_setup = zotero_setup
    app.state.db, app.state.hw, app.state.reg, app.state.llm_setup = db, hw, reg, llm_setup
    tool_cache: dict[str, Any] = {"t": 0.0, "v": []}

    # ------------------------------------------------------------------ helpers
    def project_or_404(pid: int) -> dict:
        p = db.get_project(pid)
        if not p:
            raise HTTPException(404, "Project not found")
        return p

    def section_or_404(sid: int) -> dict:
        s = db.get_section(sid)
        if not s:
            raise HTTPException(404, "Section not found")
        return s

    def section_complete(s: dict) -> bool:
        st = s["status"]
        pending = db.one("SELECT COUNT(*) c FROM revisions WHERE section_id=? AND status='pending'", (s["id"],))["c"]
        return bool((s["content"] or "").strip()) and st.get("edited") and st.get("grounded") and pending == 0

    def matrix(pid: int) -> list[dict]:
        rows = []
        for s in db.list_sections(pid):
            st = s["status"]
            rows.append({"id": s["id"], "name": s["name"], "words": len((s["content"] or "").split()),
                         "drafted": bool(st.get("drafted")), "edited": bool(st.get("edited")),
                         "grounded": bool(st.get("grounded")), "claims_fixed": bool(st.get("claims_fixed")),
                         "revised": bool(st.get("revised")), "diffs_resolved": bool(st.get("diffs_resolved")),
                         "human_edit_ratio": s["human_edit_ratio"], "complete": section_complete(s)})
        return rows

    def require_matrix(pid: int) -> None:
        if not db.get_setting("strict_gating", True):
            return
        m = matrix(pid)
        written = [r for r in m if r["words"] > 0]
        if not written or not all(r["complete"] for r in written):
            raise HTTPException(409, "Global checks unlock after Step 10: every written section must be edited "
                                     "(Step 5), grounded (Step 6) and have no pending revisions (Step 9).")

    def manuscript(pid: int) -> str:
        p = project_or_404(pid)
        return assemble_markdown(p, p["sections"])

    def hw_label() -> str:
        return hw.plan["label"]

    # ------------------------------------------------------------------ static UI
    @app.get("/", response_class=HTMLResponse)
    def index():
        html = (config.FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("{{VERSION}}", config.version()).replace("{{FOOTER}}", config.FOOTER_TEXT))

    app.mount("/static", StaticFiles(directory=str(config.FRONTEND_DIR)), name="static")
    app.mount("/assets", StaticFiles(directory=str(config.ASSETS_DIR)), name="assets")

    @app.get("/favicon.ico")
    def favicon():
        p = config.ASSETS_DIR / "favicon.png"
        return FileResponse(p) if p.exists() else Response(status_code=204)

    # ------------------------------------------------------------------ system
    @app.get("/api/health")
    def health():
        return {"status": "ok", "app": config.APP_NAME, "version": config.version(), "time": time.time()}

    @app.get("/api/network")
    def network():
        return {"online": is_online()}

    @app.get("/api/hardware/profile")
    def hardware_profile():
        return hw.snapshot()

    @app.get("/api/hardware/telemetry")
    def hardware_telemetry():
        return hw.telemetry()

    @app.post("/api/hardware/mode")
    def hardware_mode(body: dict = Body(...)):
        mode = body.get("mode", "auto")
        if mode not in MODE_LABELS:
            raise HTTPException(400, f"mode must be one of {list(MODE_LABELS)}")
        plan = hw.set_mode(mode, model_size_gb=body.get("model_size_gb"), total_layers=body.get("model_layers"),
                           manual_gpu_layers=body.get("n_gpu_layers"), reprobe=bool(body.get("reprobe")))
        db.set_setting("compute_mode", mode)
        for k, sk in (("model_size_gb", "model_size_gb"), ("model_layers", "model_layers")):
            if body.get(k):
                db.set_setting(sk, body[k])
        db.set_setting("manual_gpu_layers", body.get("n_gpu_layers"))
        return hw.snapshot() | {"plan": plan}

    @app.get("/api/tools")
    def tools(refresh: bool = False):
        if refresh or time.time() - tool_cache["t"] > 15:
            tool_cache.update(t=time.time(), v=reg.statuses())
        return {"tools": tool_cache["v"]}

    @app.get("/api/workflow")
    def workflow():
        return {"steps": STEPS, "checklist": CHECKLIST, "csl_styles": CSL_STYLES}

    @app.get("/api/about")
    def about():
        return {"app": config.APP_NAME, "version": config.version(), "creator": config.CREATOR,
                "email": config.CONTACT_EMAIL, "footer": config.FOOTER_TEXT,
                "python": platform.python_version(), "os": platform.platform(),
                "hardware": hw.profile.to_dict(), "plan": hw.plan, "tools": tools()["tools"],
                "online": is_online(), "data_dir": str(config.data_dir())}

    @app.get("/api/settings")
    def get_settings():
        return {"theme": db.get_setting("theme", "light"), "strict_gating": db.get_setting("strict_gating", True),
                "house_style": db.get_setting("house_style", "APA"), "llm_model": db.get_setting("llm_model", ""),
                "compute_mode": hw.mode}

    @app.put("/api/settings")
    def put_settings(body: dict = Body(...)):
        for k in ("theme", "strict_gating", "house_style", "llm_model"):
            if k in body:
                if k == "theme" and body[k] not in ("light", "dark"):
                    raise HTTPException(400, "theme must be light or dark")
                db.set_setting(k, body[k])
        return get_settings()

    @app.get("/api/llm/models")
    def llm_models():
        return {"backend": reg.llm.backend(), "models": reg.llm.models(), "default_model": DEFAULT_MODEL}

    # Step 4 prerequisite: install Ollama, start `ollama serve`, pull llama3.1:8b — automatically if missing
    @app.get("/api/llm/setup/status")
    def llm_setup_status():
        return llm_setup.status()

    @app.post("/api/llm/setup")
    def llm_setup_run(body: dict = Body(default={})):
        model = (body.get("model") or DEFAULT_MODEL).strip()
        if body.get("wait"):
            st = llm_setup.run(model)
        else:
            st = llm_setup.start(model)
        if st["job"]["stage"] == "ready" or st.get("ready"):
            db.set_setting("llm_model", db.get_setting("llm_model", "") or model)
        return st

    # ------------------------------------------------------------------ projects / Step 1
    @app.get("/api/projects")
    def list_projects():
        return {"projects": db.list_projects()}

    @app.post("/api/projects")
    def create_project(body: dict = Body(...)):
        title = (body.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "Title required")
        return db.create_project(title, body.get("sections"))

    @app.get("/api/projects/{pid}")
    def get_project(pid: int):
        p = project_or_404(pid)
        p["matrix"] = matrix(pid)
        p["sources"] = db.list_sources(pid)
        p["bib_entries"] = len(parse_bib(p["bib"])["entries"])
        return p

    @app.put("/api/projects/{pid}/outline")
    def save_outline(pid: int, body: dict = Body(...)):
        project_or_404(pid)
        p = db.update_project(pid, title=body.get("title"), question=body.get("question"),
                              hypothesis=body.get("hypothesis"), outline=body.get("outline"),
                              target_csl=body.get("target_csl"))
        if (p["question"] or "").strip() and (p["outline"] or "").strip():
            db.mark_step(pid, 1)
        return db.get_project(pid)

    @app.post("/api/projects/{pid}/sections")
    def add_section(pid: int, body: dict = Body(...)):
        project_or_404(pid)
        return db.add_section(pid, (body.get("name") or "Section").strip())

    @app.post("/api/projects/{pid}/steps/{n}")
    def set_step(pid: int, n: int, body: dict = Body(default={})):
        project_or_404(pid)
        return {"steps_done": db.mark_step(pid, n, bool(body.get("done", True)))}

    # ------------------------------------------------------------------ Step 2 — sources
    @app.post("/api/projects/{pid}/bib/import")
    def import_bib(pid: int, body: dict = Body(...)):
        project_or_404(pid)
        text = body.get("bib", "")
        parsed = parse_bib(text)
        merged = text if not body.get("append") else (db.get_project(pid)["bib"] + "\n\n" + text)
        db.update_project(pid, bib=merged)
        if parsed["entries"]:
            db.mark_step(pid, 2)
        return {"entries": len(parse_bib(merged)["entries"]), "errors": parsed["errors"]}

    @app.post("/api/projects/{pid}/bib/zotero")
    def zotero_sync(pid: int, body: dict = Body(default={})):
        project_or_404(pid)
        if body.get("collection") is not None:
            db.set_setting(f"zotero_collection_{pid}", body.get("collection", ""))
        res = reg.zotero.export(body.get("collection", ""), int(body.get("library_id", 1)))
        if not res.get("ok"):
            raise HTTPException(503, res["error"])
        db.update_project(pid, bib=res["bib"])
        db.mark_step(pid, 2)
        return {"entries": res["entries"], "errors": res["errors"], "engine": "native",
                "collection": body.get("collection", "")}

    @app.get("/api/projects/{pid}/bib/zotero")
    def zotero_last_collection(pid: int):
        return {"collection": db.get_setting(f"zotero_collection_{pid}", "")}

    # Step 2 prerequisite: Zotero running with Better BibTeX — installed/started automatically if missing
    @app.get("/api/zotero/setup/status")
    def zotero_setup_status():
        return zotero_setup.status()

    @app.post("/api/zotero/setup")
    def zotero_setup_run(body: dict = Body(default={})):
        return zotero_setup.run() if body.get("wait") else zotero_setup.start()

    @app.get("/api/zotero/collections")
    def zotero_collections():
        return {"collections": reg.zotero.collections()}

    @app.get("/api/projects/{pid}/bib")
    def get_bib(pid: int):
        p = project_or_404(pid)
        return {"bib": p["bib"], "entries": [{"key": e["key"], "type": e["type"], "title": e["fields"].get("title", ""),
                                              "year": e["fields"].get("year", "")}
                                             for e in parse_bib(p["bib"])["entries"]]}

    @app.post("/api/projects/{pid}/sources")
    async def upload_sources(pid: int, files: list[UploadFile] = File(...)):
        project_or_404(pid)
        dest = config.data_dir() / "uploads" / str(pid)
        dest.mkdir(parents=True, exist_ok=True)
        added = []
        for f in files:
            name = re.sub(r"[^\w.\- ]", "_", Path(f.filename or "source.pdf").name)
            if Path(name).suffix.lower() not in (".pdf", ".txt", ".md"):
                raise HTTPException(400, f"{name}: only PDF, TXT or MD sources are accepted")
            path = dest / name
            path.write_bytes(await f.read())
            added.append(db.add_source(pid, name, str(path)))
        return {"added": added, "sources": db.list_sources(pid)}

    @app.delete("/api/projects/{pid}/sources/{sid}")
    def delete_source(pid: int, sid: int):
        db.x("DELETE FROM sources WHERE id=? AND project_id=?", (sid, pid))
        return {"sources": db.list_sources(pid)}

    # ------------------------------------------------------------------ Step 3 — parse & index
    @app.post("/api/projects/{pid}/index")
    def parse_and_index(pid: int):
        project_or_404(pid)
        srcs = db.list_sources(pid, full=True)
        if not srcs:
            raise HTTPException(400, "Upload source PDFs first (Step 2).")
        futures = {s["id"]: hw.submit("cpu", reg.grobid.parse, s["path"]) for s in srcs if not s["parser"]}
        parsed = []
        for sid, fut in futures.items():
            r = fut.result()
            if r.get("ok"):
                db.update_source(sid, text=r["text"], tei=r.get("tei", ""), parsed_sections=r["sections"],
                                 parser=f"{r['engine']}:{r.get('parser', 'grobid')}")
            parsed.append({"id": sid, "engine": r.get("engine"), "ok": r.get("ok"), "note": r.get("note"),
                           "error": r.get("error"), "sections": len(r.get("sections", []))})
        srcs = db.list_sources(pid, full=True)
        idx = reg.paperqa.build_index(pid, srcs, device=hw.plan["embed_device"])
        for s in srcs:
            db.update_source(s["id"], indexed=1)
        db.mark_step(pid, 3)
        return {"parsed": parsed, "index": idx, "sources": db.list_sources(pid)}

    # ------------------------------------------------------------------ Step 4 — draft
    @app.post("/api/sections/{sid}/draft")
    def draft_section(sid: int, body: dict = Body(default={})):
        s = section_or_404(sid)
        p = project_or_404(s["project_id"])
        if not (p["question"] or "").strip() or not (p["outline"] or "").strip():
            raise HTTPException(409, "Complete Step 1 (research question + outline in your own words) before drafting.")
        keys = [e["key"] for e in parse_bib(p["bib"])["entries"]]
        prompt = draft_prompt(p, s, body.get("instructions", ""), keys)
        model = body.get("model") or db.get_setting("llm_model", "") or None
        if not model and model_present(reg.llm.models()):
            model = DEFAULT_MODEL
        events: list = []
        try:
            out = hw.submit("gpu", reg.llm.generate, prompt, plan=hw.plan, model=model, system=DRAFT_SYSTEM,
                            oom_events=events).result()
        except LLMUnavailable as exc:
            raise HTTPException(503, f"{exc} ResearchMate can install and start it for you: open Step 4 or "
                                     f"POST /api/llm/setup (installs Ollama, runs `ollama serve`, pulls {DEFAULT_MODEL}).")
        except RuntimeError as exc:
            raise HTTPException(502, f"LLM error: {exc}")
        hw.oom_events.extend(events)
        text = out["text"]
        db.save_section_content(sid, text, by_ai=True)
        db.set_section_status(sid, drafted=True, edited=False, grounded=False, claims_fixed=False)
        db.log_ai(project_id=p["id"], section_id=sid, step=4, tool=f"Local LLM ({out['backend']})", model=out["model"],
                  hardware_mode=hw_label(), prompt=prompt, words=len(text.split()))
        db.mark_step(p["id"], 4)
        return {"section": db.get_section(sid), "model": out["model"], "backend": out["backend"],
                "seconds": out["seconds"], "hardware": hw_label(), "options": out["options"], "oom_events": events}

    # ------------------------------------------------------------------ Step 5 — human edit
    @app.get("/api/sections/{sid}")
    def get_section(sid: int):
        return section_or_404(sid)

    @app.put("/api/sections/{sid}")
    def edit_section(sid: int, body: dict = Body(...)):
        s = section_or_404(sid)
        content = body.get("content", "")
        if content == s["content"] and not body.get("mark_edited"):
            return s
        changed = content != s["content"]
        s = db.save_section_content(sid, content, by_ai=False)
        if body.get("name"):
            db.x("UPDATE sections SET name=? WHERE id=?", (body["name"], sid))
        db.set_section_status(sid, edited=True, grounded=False if changed else s["status"].get("grounded", False))
        db.mark_step(s["project_id"], 5)
        return db.get_section(sid)

    # ------------------------------------------------------------------ Step 6/7 — grounding
    @app.post("/api/sections/{sid}/ground")
    def ground_section(sid: int):
        s = section_or_404(sid)
        res = hw.submit("cpu", reg.paperqa.verify, s["project_id"], s["content"] or "", hw.plan["embed_device"]).result()
        if not res.get("ok"):
            raise HTTPException(409, res.get("error", "Grounding failed"))
        db.save_finding(s["project_id"], 6, {"section_id": sid, **res})
        unsupported = res["summary"]["unsupported"]
        db.set_section_status(sid, grounded=True, claims_fixed=unsupported == 0)
        db.mark_step(s["project_id"], 6)
        if unsupported == 0:
            db.mark_step(s["project_id"], 7)
        return res

    @app.get("/api/sections/{sid}/claims")
    def claims_queue(sid: int):
        s = section_or_404(sid)
        rows = db.q("SELECT payload FROM findings WHERE project_id=? AND step=6 ORDER BY id DESC", (s["project_id"],))
        for r in rows:
            pl = json.loads(r["payload"])
            if pl.get("section_id") == sid:
                return {"queue": [c for c in pl["claims"] if c["status"] != "supported"], "summary": pl["summary"]}
        return {"queue": [], "summary": None}

    @app.post("/api/sections/{sid}/claims/remove")
    def remove_claim(sid: int, body: dict = Body(...)):
        s = section_or_404(sid)
        claim = body.get("claim", "")
        if not claim or claim not in (s["content"] or ""):
            raise HTTPException(404, "Claim text not found in section (it may already be edited).")
        new = re.sub(r"\s{2,}", " ", s["content"].replace(claim, "", 1)).strip()
        db.save_section_content(sid, new, by_ai=False)
        db.set_section_status(sid, grounded=False)
        return db.get_section(sid)

    @app.post("/api/sections/{sid}/claims/resolve")
    def resolve_claims(sid: int):
        s = section_or_404(sid)
        db.set_section_status(sid, claims_fixed=True)
        db.mark_step(s["project_id"], 7)
        return db.get_section(sid)

    # ------------------------------------------------------------------ Step 8/9 — revise & diff
    @app.post("/api/sections/{sid}/revise")
    def revise_section(sid: int, body: dict = Body(default={})):
        s = section_or_404(sid)
        engine = body.get("engine", "xtragpt")
        if engine not in ("xtragpt", "paperdebugger"):
            raise HTTPException(400, "engine must be xtragpt or paperdebugger")
        text = s["content"] or ""
        if not text.strip():
            raise HTTPException(400, "Section is empty.")
        ctx = manuscript(s["project_id"])
        if engine == "xtragpt":
            gpu_task = lambda: reg.xtragpt.revise(text, ctx, hw.plan, body.get("instruction", ""))  # noqa: E731
        else:
            gpu_task = lambda: reg.paperdebugger.revise(text, s["name"], hw.plan, body.get("instruction", ""))  # noqa: E731
        # Hybrid task parallelism: revision on the GPU lane while linting runs on CPU workers.
        par = hw.run_parallel({"revision": gpu_task},
                              {"harper": lambda: reg.harper.check(text),
                               "vale": lambda: reg.vale.lint(text, db.get_setting("house_style", "APA"))})
        if not par["revision"]["ok"]:
            raise HTTPException(502, par["revision"]["error"])
        rev = par["revision"]["result"]
        hunks = make_hunks(text, rev["revised"])
        r = db.add_revision(sid, engine, text, rev["revised"], hunks, rev["notes"])
        if rev["engine"] == "native":
            db.log_ai(project_id=s["project_id"], section_id=sid, step=8,
                      tool="XtraGPT" if engine == "xtragpt" else "PaperDebugger", model=rev["model"],
                      hardware_mode=hw_label(), prompt=rev["prompt"], words=len(rev["revised"].split()))
        if not hunks:
            db.set_revision_status(r["id"], "applied")
        db.set_section_status(sid, revised=True, diffs_resolved=not hunks)
        db.mark_step(s["project_id"], 8)
        return {"revision": db.get_revision(r["id"]), "engine": rev["engine"], "model": rev["model"],
                "parallel": {k: {"ok": v["ok"], "issues": len(v["result"]["issues"]) if v["ok"] else None}
                             for k, v in par.items() if k != "revision"},
                "hardware": hw_label()}

    @app.get("/api/revisions/{rid}")
    def get_revision(rid: int):
        r = db.get_revision(rid)
        if not r:
            raise HTTPException(404, "Revision not found")
        return r

    @app.get("/api/sections/{sid}/revisions")
    def list_revisions(sid: int):
        return {"revisions": [db.get_revision(r["id"]) for r in
                              db.q("SELECT id FROM revisions WHERE section_id=? ORDER BY id DESC", (sid,))]}

    @app.post("/api/revisions/{rid}/apply")
    def apply_revision(rid: int, body: dict = Body(...)):
        r = db.get_revision(rid)
        if not r:
            raise HTTPException(404, "Revision not found")
        if r["status"] != "pending":
            raise HTTPException(409, "Revision already resolved")
        s = section_or_404(r["section_id"])
        if s["content"] != r["original"]:
            raise HTTPException(409, "Section changed since this revision was proposed — request a new revision.")
        decisions = body.get("decisions", {})
        new = apply_decisions(r["original"], r["revised"], decisions)
        accepted_chars = sum(len(h["after"]) for h in r["hunks"] if decisions.get(str(h["id"]), decisions.get(h["id"])) == "accept")
        db.x("UPDATE sections SET content=?, ai_chars=ai_chars+?, updated=? WHERE id=?",
             (new, accepted_chars, time.time(), r["section_id"]))
        db.set_revision_status(rid, "applied")
        db.set_section_status(r["section_id"], diffs_resolved=True,
                              grounded=s["status"].get("grounded", False) and new == s["content"])
        db.mark_step(s["project_id"], 9)
        acc = sum(1 for v in decisions.values() if v == "accept")
        return {"section": db.get_section(r["section_id"]), "accepted": acc, "rejected": len(r["hunks"]) - acc}

    # ------------------------------------------------------------------ Step 10 — matrix
    @app.get("/api/projects/{pid}/matrix")
    def get_matrix(pid: int):
        project_or_404(pid)
        m = matrix(pid)
        written = [r for r in m if r["words"]]
        done = bool(written) and all(r["complete"] for r in written)
        if done:
            db.mark_step(pid, 10)
        return {"matrix": m, "complete": done}

    # ------------------------------------------------------------------ Step 11/12/13 — linting
    def per_section(pid: int, fn) -> list[dict]:
        out = []
        futs = [(s, hw.submit("cpu", fn, s["content"] or "")) for s in db.list_sections(pid) if (s["content"] or "").strip()]
        for s, f in futs:
            out.append({"section_id": s["id"], "name": s["name"], "result": f.result()})
        return out

    @app.post("/api/projects/{pid}/grammar")
    def grammar(pid: int, body: dict = Body(default={})):
        project_or_404(pid)
        require_matrix(pid)
        engine = body.get("engine", "harper")
        fn = reg.harper.check if engine == "harper" else reg.languagetool.check if engine == "languagetool" else None
        if fn is None:
            raise HTTPException(400, "engine must be harper or languagetool")
        res = per_section(pid, fn)
        db.save_finding(pid, 11, res)
        db.mark_step(pid, 11)
        return {"engine": engine, "sections": res}

    @app.post("/api/projects/{pid}/style")
    def style(pid: int, body: dict = Body(default={})):
        project_or_404(pid)
        require_matrix(pid)
        engine = body.get("engine", "vale")
        house = body.get("house_style") or db.get_setting("house_style", "APA")
        out: dict[str, Any] = {"engine": engine}
        if engine in ("vale", "both"):
            out["vale"] = per_section(pid, lambda t: reg.vale.lint(t, house))
        if engine in ("humanizer", "both"):
            dev = hw.plan["humanizer_device"]
            out["humanizer"] = per_section(pid, lambda t: reg.humanizer.analyze(t, dev))
        if len(out) == 1:
            raise HTTPException(400, "engine must be vale, humanizer or both")
        db.save_finding(pid, 12, out)
        db.mark_step(pid, 12)
        return out

    @app.post("/api/analyze")
    def analyze(body: dict = Body(...)):
        t = body.get("text", "")
        return {"readability": readability(t), "humanizer": reg.humanizer.analyze(t, "cpu"),
                "vale": reg.vale.lint(t, db.get_setting("house_style", "APA"))}

    # ------------------------------------------------------------------ Step 14/15 — references
    @app.post("/api/projects/{pid}/refcheck")
    def refcheck(pid: int):
        p = project_or_404(pid)
        require_matrix(pid)
        res = reg.refchecker.check(p["bib"])
        db.save_finding(pid, 14, res)
        if res["online"]:
            db.mark_step(pid, 14)
        return res

    @app.post("/api/projects/{pid}/citations")
    def citations(pid: int):
        p = project_or_404(pid)
        require_matrix(pid)
        ms = manuscript(pid)
        out = {"citescan": reg.citescan.scan(ms, p["bib"]), "check_citations": reg.check_citations.validate(p["bib"])}
        db.save_finding(pid, 15, out)
        if out["citescan"]["parity"] and out["check_citations"]["valid"]:
            db.mark_step(pid, 15)
        return out

    @app.post("/api/projects/{pid}/bib/clean")
    def clean_bib(pid: int):
        p = project_or_404(pid)
        res = CiteScanAdapter.clean(manuscript(pid), p["bib"])
        db.update_project(pid, bib=res["bib"])
        return res

    # ------------------------------------------------------------------ Step 16/17/18
    @app.post("/api/projects/{pid}/overlap")
    def overlap(pid: int):
        project_or_404(pid)
        require_matrix(pid)
        res = source_overlap(manuscript(pid), db.list_sources(pid, full=True))
        db.save_finding(pid, 16, res)
        if not res["flagged"]:
            db.mark_step(pid, 16)
        return res

    @app.post("/api/projects/{pid}/consistency")
    def consistency(pid: int):
        p = project_or_404(pid)
        require_matrix(pid)
        res = reg.paper_analysis.audit(p["sections"])
        db.save_finding(pid, 17, res)
        if res["consistent"]:
            db.mark_step(pid, 17)
        return res

    @app.get("/api/projects/{pid}/disclosure")
    def disclosure(pid: int):
        p = project_or_404(pid)
        d = build_disclosure(p, db.ai_log(pid), p["sections"])
        db.mark_step(pid, 18)
        return d

    @app.get("/api/projects/{pid}/ailog")
    def ailog(pid: int):
        project_or_404(pid)
        return {"log": db.ai_log(pid)}

    # ------------------------------------------------------------------ Step 19 — preview & export
    @app.post("/api/projects/{pid}/preview")
    def preview(pid: int, body: dict = Body(default={})):
        p = project_or_404(pid)
        csl = body.get("csl") or p["target_csl"] or "apa"
        return reg.typst.preview(manuscript(pid), p["bib"], csl, p["title"])

    def exports_dir(pid: int) -> Path:
        d = config.data_dir() / "exports" / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        return d

    @app.post("/api/projects/{pid}/export")
    def export(pid: int, body: dict = Body(default={})):
        p = project_or_404(pid)
        require_matrix(pid)
        fmts = body.get("formats") or [body.get("format", "docx")]
        csl = body.get("csl") or p["target_csl"] or "apa"
        if csl not in CSL_STYLES:
            raise HTTPException(400, f"csl must be one of {list(CSL_STYLES)}")
        version = next_version(exports_dir(pid), "Manuscript")
        results = []
        for fmt in fmts:
            try:
                results.append(reg.pandoc.export(manuscript(pid), p["bib"], csl, p["title"], fmt, exports_dir(pid), version))
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        if any(r.get("ok") for r in results):
            db.update_project(pid, export_version=version, target_csl=csl)
            db.mark_step(pid, 19)
        return {"version": version, "results": results}

    @app.get("/api/projects/{pid}/exports")
    def list_exports(pid: int):
        return {"files": sorted(f.name for f in exports_dir(pid).iterdir() if f.is_file())}

    @app.get("/api/files/{pid}/{name}")
    def download(pid: int, name: str):
        path = (exports_dir(pid) / name).resolve()
        if path.parent != exports_dir(pid).resolve() or not path.exists():
            raise HTTPException(404, "File not found")
        return FileResponse(path, filename=name)

    # ------------------------------------------------------------------ Step 20/21
    @app.post("/api/projects/{pid}/figures")
    async def upload_figures(pid: int, files: list[UploadFile] = File(...)):
        project_or_404(pid)
        d = config.data_dir() / "uploads" / str(pid) / "figures"
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            (d / re.sub(r"[^\w.\- ]", "_", Path(f.filename or "figure").name)).write_bytes(await f.read())
        return {"figures": sorted(x.name for x in d.iterdir())}

    @app.post("/api/projects/{pid}/submit")
    def submit(pid: int, body: dict = Body(default={})):
        p = project_or_404(pid)
        require_matrix(pid)
        ex = exports_dir(pid)
        version = p["export_version"]
        if not version or not list(ex.glob(f"Manuscript_v{version}.*")):
            version = export(pid, {"formats": ["docx", "tex"], "csl": p["target_csl"] or "apa"})["version"]
        clean = CiteScanAdapter.clean(manuscript(pid), p["bib"])
        disc = build_disclosure(p, db.ai_log(pid), p["sections"])
        bundle = ex / f"Submission_Bundle_v{version}.zip"
        fig_dir = config.data_dir() / "uploads" / str(pid) / "figures"
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
            for f in ex.glob(f"Manuscript_v{version}.*"):
                z.write(f, f.name)
            z.writestr("references.bib", clean["bib"])
            z.writestr("AI_Use_Disclosure.md", f"# AI-Use Disclosure Statement\n\n{disc['statement']}\n\n"
                       "## Audit log\n\n| When | Step | Section | Tool | Model | Hardware | Words |\n|---|---|---|---|---|---|---|\n"
                       + "".join(f"| {r['when']} | {r['step']} | {r['section']} | {r['tool']} | {r['model']} | "
                                 f"{r['hardware']} | {r['words']} |\n" for r in disc["log"]))
            z.writestr("Pre-submission_Checklist.md", "# Pre-submission checklist\n\n" +
                       "".join(f"- [{'x' if i in (body.get('checked') or []) else ' '}] {c}\n"
                               for i, c in enumerate(CHECKLIST)))
            if fig_dir.exists():
                for f in fig_dir.iterdir():
                    z.write(f, f"figures/{f.name}")
        db.mark_step(pid, 21)
        return {"bundle": bundle.name, "version": version, "contents": zipfile.ZipFile(bundle).namelist(),
                "url": f"/api/files/{pid}/{bundle.name}"}

    @app.exception_handler(Exception)
    async def unhandled(_req, exc: Exception):  # never show a raw stack trace in the browser
        return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})

    return app


def get_app() -> FastAPI:
    return create_app()
