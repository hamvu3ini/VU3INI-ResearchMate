"""Install Ollama if missing, start `ollama serve`, and pull llama3.1:8b (or import the bundled GGUF offline).

  python scripts/setup_llm.py                 # default model llama3.1:8b
  python scripts/setup_llm.py --model qwen3:8b

Used by install_offline.bat; the same logic runs from Step 4 in the browser.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.adapters.ollama_setup import DEFAULT_MODEL, OllamaSetup  # noqa: E402


class ConsoleSetup(OllamaSetup):
    _last = ""

    def _set(self, stage=None, progress=None, message=None):
        super()._set(stage, progress, message)
        line = f"  [{self.job['stage']:<10}] {self.job['message']}"
        if progress is not None and progress >= 0 and self.job["stage"] == "pulling":
            line += f"  {progress * 100:5.1f}%"
        if line != self._last:
            end = "\r" if self.job["stage"] == "pulling" and progress not in (None, 1.0) else "\n"
            print(line.ljust(110), end=end, flush=True)
            self._last = line


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()
    print(f"\n  Local LLM setup: Ollama + {args.model}")
    st = ConsoleSetup().run(args.model)
    print()
    if st["job"]["stage"] == "ready":
        print(f"  Local LLM ready: {args.model} via Ollama on 127.0.0.1:11434")
        return 0
    print(f"  Local LLM setup did not finish: {st['job']['error']}")
    print("  ResearchMate still works; Step 4 will retry this automatically.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
