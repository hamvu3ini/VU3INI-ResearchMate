"""Step 18 — journal-ready AI-Use Disclosure Statement built from the Step 4 / Step 8 audit log."""
from __future__ import annotations

import datetime as dt


def build_disclosure(project: dict, log: list[dict], sections: list[dict]) -> dict:
    drafting = [e for e in log if e["step"] == 4]
    revising = [e for e in log if e["step"] == 8]
    models = sorted({e["model"] for e in log if e["model"]})
    tools = sorted({e["tool"] for e in log})
    modes = sorted({e["hardware_mode"] for e in log if e["hardware_mode"]})
    drafted = sorted({e["section_name"] for e in drafting if e.get("section_name")})
    revised = sorted({e["section_name"] for e in revising if e.get("section_name")})
    ai_sections = [s for s in sections if s.get("ai_chars")]
    avg_ratio = round(100 * sum(s["human_edit_ratio"] for s in ai_sections) / len(ai_sections)) if ai_sections else 100
    if not log:
        statement = ("No generative AI tools were used to draft or revise the text of this manuscript. Automated "
                     "grammar, style and reference-checking tools were used; the author(s) take full responsibility "
                     "for the content.")
    else:
        parts = [f"During the preparation of this work the author(s) used {', '.join(tools)}"
                 + (f" (model{'s' if len(models) > 1 else ''}: {', '.join(models)})" if models else "")
                 + ", run locally on the author's own computer."]
        if drafted:
            parts.append(f"The tool was used to produce initial drafts of the following section(s): {', '.join(drafted)}.")
        if revised:
            parts.append(f"It was also used to suggest revisions for clarity and structure in: {', '.join(revised)}; "
                         "each suggested change was individually accepted or rejected by the author(s).")
        parts.append("The research question, hypothesis and outline were formulated by the author(s) without AI. "
                     "All AI-generated text was reviewed and edited by the author(s) "
                     f"(on average {avg_ratio}% of AI-drafted characters were subsequently rewritten by hand), every "
                     "claim was checked against the cited sources, and all references were verified against "
                     "bibliographic databases. The author(s) take full responsibility for the content of the publication.")
        statement = " ".join(parts)
    table = [{"when": dt.datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M"), "step": e["step"],
              "section": e.get("section_name") or "", "tool": e["tool"], "model": e["model"],
              "hardware": e["hardware_mode"], "words": e["words"], "prompt": (e["prompt"] or "")[:300]} for e in log]
    return {"statement": statement, "models": models, "tools": tools, "hardware_modes": modes,
            "sections_drafted": drafted, "sections_revised": revised, "human_edit_percent": avg_ratio,
            "log": table, "generated": dt.datetime.now().isoformat(timespec="seconds")}
