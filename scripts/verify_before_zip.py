"""Stages 1 + 2 of build_installer.bat — nothing gets zipped unless both pass.

Stage 1  pytest tests/test_researchmate_e2e.py -v                (hard gate)
Stage 2  start the real server, open it in the DEFAULT browser, HTTP-check /, /api/health,
         /api/hardware/profile, /assets/logo.png, run live API round-trips, then ask the developer
         to confirm the UI works  ([Y] build ZIP / [N] abort;  --auto-confirm for unattended builds)

Exit code 0 = OK to package, anything else = abort.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
GREEN, RED, YEL, OFF = ("\033[92m", "\033[91m", "\033[93m", "\033[0m") if sys.stdout.isatty() else ("", "", "", "")


def banner(msg: str) -> None:
    print(f"\n{'=' * 78}\n  {msg}\n{'=' * 78}", flush=True)


def ok(msg: str) -> None:
    print(f"  {GREEN}[PASS]{OFF} {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"  {RED}[FAIL]{OFF} {msg}", flush=True)


def http(method: str, url: str, body: dict | None = None, timeout: float = 30) -> tuple[int, bytes, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("content-type", "")


# ---------------------------------------------------------------- stage 1
def stage1() -> bool:
    banner("STAGE 1/3 — Automated functional & error test suite (pytest)")
    cmd = [PY, "-m", "pytest", str(ROOT / "tests" / "test_researchmate_e2e.py"), "-v", "--color=yes"]
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        fail(f"pytest exited with code {rc} — build HALTED, no ZIP will be created.")
        return False
    ok("All tests passed.")
    return True


# ---------------------------------------------------------------- stage 2
def stage2(port: int, auto_confirm: bool, open_browser: bool) -> bool:
    banner("STAGE 2/3 — Live default-browser execution & smoke verification")
    data_dir = Path(tempfile.mkdtemp(prefix="researchmate_verify_"))
    env = dict(os.environ, RESEARCHMATE_DATA=str(data_dir), PYTHONUTF8="1")
    flags = 0x00000200 if sys.platform.startswith("win") else 0  # CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen([PY, str(ROOT / "run.py"), "--no-browser", "--port", str(port)], cwd=str(ROOT), env=env,
                            creationflags=flags)
    url_file = data_dir / "logs" / "last_url.txt"
    passed = True
    try:
        t0 = time.time()
        while not url_file.exists() and time.time() - t0 < 30:
            time.sleep(0.3)
        url = url_file.read_text().strip() if url_file.exists() else f"http://127.0.0.1:{port}"
        healthy = False
        while time.time() - t0 < 90:
            try:
                if http("GET", url + "/api/health", timeout=3)[0] == 200:
                    healthy = True
                    break
            except OSError:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        if not healthy:
            fail(f"Server did not become healthy at {url}")
            return False
        ok(f"Server healthy at {url}")

        checks = []

        def check(name: str, cond: bool, detail: str = "") -> None:
            nonlocal passed
            checks.append(name)
            (ok if cond else fail)(f"{name}{' — ' + detail if detail and not cond else ''}")
            passed = passed and cond

        s, b, ct = http("GET", url + "/")
        html = b.decode("utf-8", "replace")
        check("GET /  → 200", s == 200)
        check("DOM: logo top-left (#brand-logo → /assets/logo.png)", 'id="brand-logo"' in html and "/assets/logo.png" in html)
        check("DOM: title 'VU3INI ResearchMate'", "VU3INI ResearchMate" in html)
        check("DOM: footer 'By Sasanka Boruah -VU3INI'", ">By Sasanka Boruah -VU3INI<" in html)
        check("DOM: no CDN references", "https://cdn" not in html and "http://cdn" not in html)
        s, b, _ = http("GET", url + "/api/health")
        check("GET /api/health → 200", s == 200 and json.loads(b)["status"] == "ok")
        s, b, _ = http("GET", url + "/api/hardware/profile")
        hw = json.loads(b) if s == 200 else {}
        check("GET /api/hardware/profile → 200", s == 200 and "plan" in hw)
        if hw:
            print(f"         detected: {hw['profile']['cpu_model']} · {hw['profile']['physical_cores']} cores · "
                  f"{hw['profile']['ram_total_gb']} GB RAM · GPUs: "
                  f"{', '.join(g['name'] for g in hw['profile']['gpus']) or 'none'}")
            print(f"         plan    : {hw['plan']['label']}")
        s, b, ct = http("GET", url + "/assets/logo.png")
        check("GET /assets/logo.png → 200 image/png", s == 200 and "image/png" in ct and b[:8] == b"\x89PNG\r\n\x1a\n")
        for path in ("/static/app.js", "/static/styles.css", "/api/workflow", "/api/tools", "/api/about"):
            check(f"GET {path} → 200", http("GET", url + path)[0] == 200)

        # live API round-trips
        s, b, _ = http("POST", url + "/api/projects", {"title": "Verification paper", "sections": ["Abstract", "Results"]})
        proj = json.loads(b)
        check("POST /api/projects", s == 200 and proj.get("id"))
        pid = proj["id"]
        s, _, _ = http("PUT", url + f"/api/projects/{pid}/outline", {"question": "Q?", "outline": "1. A", "hypothesis": "H"})
        check("PUT outline (Step 1)", s == 200)
        bib = (ROOT / "tests" / "fixtures" / "refs.bib").read_text(encoding="utf-8")
        s, b, _ = http("POST", url + f"/api/projects/{pid}/bib/import", {"bib": bib})
        check("POST bib import (Step 2)", s == 200 and json.loads(b)["entries"] == 3)
        sid = proj["sections"][1]["id"]
        s, b, _ = http("PUT", url + f"/api/sections/{sid}", {"content": "Trees cool streets by 2.4 degrees [@smith2020]."})
        check("PUT section edit (Step 5)", s == 200 and json.loads(b)["status"]["edited"])
        s, b, _ = http("POST", url + "/api/analyze", {"text": "We delve into the data. We delve into it again."})
        check("POST /api/analyze (Vale + Humanizer)", s == 200 and json.loads(b)["vale"]["issues"])
        s, b, _ = http("POST", url + f"/api/projects/{pid}/preview", {"csl": "apa"})
        check("POST preview (Typst / HTML)", s == 200 and json.loads(b)["ok"])
        for mode in ("cpu", "hybrid", "gpu", "auto"):
            s, b, _ = http("POST", url + "/api/hardware/mode", {"mode": mode})
            check(f"POST hardware mode → {mode}", s == 200 and json.loads(b)["mode"] == mode)
        s, b, _ = http("PUT", url + "/api/settings", {"theme": "dark"})
        check("PUT theme dark", s == 200 and json.loads(b)["theme"] == "dark")
        http("PUT", url + "/api/settings", {"theme": "light"})
        s, b, _ = http("POST", url + f"/api/projects/{pid}/grammar", {"engine": "harper"})
        check("Global-step gating returns 409 before Step 10", s == 409)

        if not passed:
            fail("Live smoke checks failed — build HALTED.")
            return False

        if open_browser:
            print(f"\n  Opening {url} in your DEFAULT web browser…")
            webbrowser.open(url)
        print(f"\n  {len(checks)} live checks passed.")
        if auto_confirm:
            ok("--auto-confirm given: skipping manual UI confirmation.")
            return True
        while True:
            ans = input(f"\n  {YEL}Press [Y] after confirming the browser UI works to build the ZIP, or [N] to abort: {OFF}")
            if ans.strip().lower() in ("y", "yes"):
                ok("Developer confirmed the browser UI.")
                return True
            if ans.strip().lower() in ("n", "no"):
                fail("Developer aborted the build.")
                return False
    finally:
        print("  Shutting down verification server…", flush=True)
        if sys.platform.startswith("win"):
            subprocess.call(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto-confirm", action="store_true", help="unattended build: skip the Y/N prompt")
    ap.add_argument("--no-browser", action="store_true", help="do not open the default browser in stage 2")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--skip-tests", action="store_true", help=argparse.SUPPRESS)  # never used by build_installer.bat
    args, _unknown = ap.parse_known_args()
    if not args.skip_tests and not stage1():
        return 1
    if not stage2(args.port, args.auto_confirm, not args.no_browser):
        return 2
    banner("Stages 1 and 2 PASSED — packaging may proceed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
