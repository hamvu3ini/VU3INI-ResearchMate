"""Start VU3INI ResearchMate and open it in the user's DEFAULT web browser.

  python run.py                 # 127.0.0.1:8765 (or the next free port), opens the browser
  python run.py --no-browser    # server only (used by the verification harness)
  python run.py --port 9000
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def free_port(host: str, start: int, tries: int = 30) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, port)) != 0:
                return port
    raise RuntimeError(f"No free port between {start} and {start + tries}")


def wait_healthy(url: str, timeout: float = 60) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except OSError:
            time.sleep(0.4)
    return False


def main() -> None:
    from backend import config
    ap = argparse.ArgumentParser(description=config.APP_NAME)
    ap.add_argument("--host", default=config.DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    import uvicorn
    from backend.main import create_app

    port = free_port(args.host, args.port)
    url = f"http://{args.host}:{port}"
    (config.data_dir() / "logs" / "last_url.txt").write_text(url, encoding="utf-8")
    print(f"\n  {config.APP_NAME} v{config.version()}  —  {config.FOOTER_TEXT}")
    print(f"  Serving at {url}   (Ctrl+C to stop)\n")

    # If Ollama is installed but not running, start `ollama serve` now so Step 4 is ready (no install here;
    # installing + pulling llama3.1:8b happens from Step 4 / install_offline.bat).
    def start_llm():
        try:
            from backend.adapters.ollama_setup import OllamaSetup
            if OllamaSetup().ensure_server_started():
                print("  Local LLM: Ollama server running (127.0.0.1:11434)")
        except Exception as exc:  # noqa: BLE001
            print(f"  Local LLM: could not start Ollama ({exc})")
    threading.Thread(target=start_llm, daemon=True).start()

    if not args.no_browser:
        def opener():
            if wait_healthy(url):
                webbrowser.open(url)   # the user's default browser — Edge, Chrome, Firefox or Brave
            else:
                print("  Server did not become healthy in time; open the URL manually.")
        threading.Thread(target=opener, daemon=True).start()

    os.environ.setdefault("PYTHONUTF8", "1")
    uvicorn.run(create_app(), host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
