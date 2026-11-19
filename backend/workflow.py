"""The 21-step guided pipeline with per-step "What Each Tool Option Does" explainer cards."""
from __future__ import annotations

STEPS: list[dict] = [
    {"n": 1, "title": "Define Research Question & Outline", "panel": "outline", "ai": False,
     "desc": "Formulate the research question, hypothesis and outline in your own words. Generative AI is disabled here "
             "so the framing of the paper is yours."},
    {"n": 2, "title": "Collect Sources & Export .bib", "panel": "sources",
     "desc": "Sync from Zotero via Better BibTeX or import a .bib file, then upload source PDFs.",
     "options": [
         {"tool": "Zotero + Better BibTeX (live sync)", "does": "Pulls a whole library or one collection straight from a "
          "running Zotero with stable citation keys.", "best": "You keep adding papers in Zotero while writing."},
         {"tool": "Manual .bib import", "does": "Paste or upload a .bib exported from any manager (Zotero, Mendeley, JabRef, "
          "Google Scholar).", "best": "Offline work, or a fixed bibliography you received from a co-author."}]},
    {"n": 3, "title": "Parse & Index Source PDFs", "panel": "index",
     "desc": "Extract structure with GROBID and index passages for claim verification.",
     "options": [
         {"tool": "GROBID", "does": "Machine-learning PDF parser producing TEI/XML: title, sections, tables, reference list.",
          "best": "Knowing *which section* of a paper a passage came from."},
         {"tool": "PaperQA2 indexing", "does": "Splits parsed text into passages and builds a retrieval index (BM25, plus "
          "dense embeddings on your GPU/CPU when an embedding model is vendored).", "best": "Finding supporting evidence for a claim in Step 6."}]},
    {"n": 4, "title": "Draft One Section with Local LLM", "panel": "draft", "ai": True,
     "desc": "Draft a single section with the local LLM using the active compute profile. Model, hardware mode, prompt and "
             "time are written to the AI-Use Log."},
    {"n": 5, "title": "Read & Edit the Section Yourself", "panel": "edit",
     "desc": "Human-in-the-loop editing. ResearchMate tracks how much of the AI draft you rewrote (human-vs-AI edit ratio)."},
    {"n": 6, "title": "Run Literature Grounding Check", "panel": "ground",
     "desc": "Every factual sentence is checked against your indexed PDFs; each claim gets an evidence passage and a verdict."},
    {"n": 7, "title": "Fix or Remove Unsupported Claims", "panel": "claims",
     "desc": "A guided queue of unsupported and partially supported claims: add evidence, soften, or delete."},
    {"n": 8, "title": "Revise with XtraGPT / PaperDebugger", "panel": "revise", "ai": True,
     "desc": "Ask for a revision proposal. Nothing changes until you accept hunks in Step 9.",
     "options": [
         {"tool": "XtraGPT", "does": "Sentence-level academic polishing: clarity, concision, scholarly flow — keeps your claims.",
          "best": "Final language polish of a section whose argument is already sound."},
         {"tool": "PaperDebugger", "does": "Reviewer-style critique: argument gaps, unsupported claims, weak transitions, "
          "missing methodological detail — then a fix proposal.", "best": "Before polishing; when a section feels logically thin."}]},
    {"n": 9, "title": "Accept or Reject Revision Diffs", "panel": "diff",
     "desc": "Side-by-side diff. Accept or reject every change individually."},
    {"n": 10, "title": "Repeat Steps 4–9 for Every Section", "panel": "matrix",
     "desc": "Section-by-section completion matrix. Global checks unlock when every section has been edited and grounded."},
    {"n": 11, "title": "Grammar & Spelling Check", "panel": "grammar", "global": True,
     "options": [
         {"tool": "Harper", "does": "Rust-based checker running locally in milliseconds: spelling, a/an, repeated words, spacing, capitalisation.",
          "best": "Fast first pass and real-time checking while you type."},
         {"tool": "LanguageTool", "does": "Thousands of grammar rules plus n-gram context: agreement, confusable words, punctuation, style.",
          "best": "Thorough final grammar pass."}],
     "desc": "Choose a grammar engine and fix issues in place."},
    {"n": 12, "title": "House Style & AI-Pattern Linting", "panel": "style", "global": True,
     "options": [
         {"tool": "Vale", "does": "Rule-based prose linter: IEEE / APA / Nature house style plus AI-cliché, hedging and wordiness rules (editable YAML).",
          "best": "Matching a journal's style guide and removing stock AI phrases."},
         {"tool": "Turnitout-Humanizer", "does": "Stylometric analysis: sentence-length burstiness, repeated openers, vocabulary diversity — flags robotic cadence.",
          "best": "Finding passages that *sound* machine-written even when every word is fine."}],
     "desc": "Lint for house style and AI-typical patterns."},
    {"n": 13, "title": "Rewrite Flagged Passages Yourself", "panel": "rewrite", "global": True,
     "desc": "Focused rewrite workbench with live readability and burstiness feedback."},
    {"n": 14, "title": "Verify All References (RefChecker)", "panel": "refcheck", "global": True,
     "desc": "Every reference is checked against Crossref, Semantic Scholar, OpenAlex and arXiv (needs internet)."},
    {"n": 15, "title": "Match Citations & Clean .bib", "panel": "citations", "global": True,
     "options": [
         {"tool": "CiteScan", "does": "Compares in-text citation keys with .bib entries: cited-but-missing and unused entries; one-click cleanup.",
          "best": "Making the bibliography exactly match the manuscript."},
         {"tool": "check-citations", "does": "Static .bib validation: syntax, duplicate keys, required fields, crossrefs, DOI/year format.",
          "best": "Preventing export failures and malformed reference lists."}],
     "desc": "Make in-text citations and the .bib match 1-to-1."},
    {"n": 16, "title": "Source Overlap Check", "panel": "overlap", "global": True,
     "desc": "Detect verbatim or close-paraphrase overlap with your source PDFs; quote or rewrite."},
    {"n": 17, "title": "Cross-Section Consistency", "panel": "consistency", "global": True,
     "desc": "Paper Analysis Utility: numbers, figures, tables and acronyms consistent between Abstract and Results."},
    {"n": 18, "title": "Generate AI-Use Disclosure", "panel": "disclosure", "global": True,
     "desc": "Journal-ready statement generated from the Step 4 and Step 8 audit logs."},
    {"n": 19, "title": "Preview (Typst) & Export (Pandoc)", "panel": "export", "global": True,
     "options": [
         {"tool": "Typst", "does": "Instant typeset preview in the browser (SVG pages) while you write.",
          "best": "Checking layout and citations quickly."},
         {"tool": "Pandoc + citeproc", "does": "Final conversion to DOCX, LaTeX or PDF with citations formatted by the journal's CSL style.",
          "best": "The files you actually submit."}],
     "desc": "Preview, then export versioned DOCX / LaTeX / PDF."},
    {"n": 20, "title": "Final Read-Through", "panel": "read", "global": True,
     "desc": "Distraction-free reader with inline edits and a pre-submission checklist."},
    {"n": 21, "title": "Submit", "panel": "submit", "global": True,
     "desc": "Package manuscript, cleaned .bib, figures and the AI-Use Disclosure into Submission_Bundle_vX.Y.Z.zip."},
]

CHECKLIST = [
    "Title, abstract and keywords match the journal's limits",
    "Every claim is supported or cited (Step 6/7)",
    "All references verified (Step 14) and .bib parity achieved (Step 15)",
    "No unquoted verbatim overlap with sources (Step 16)",
    "Abstract numbers match Results (Step 17)",
    "AI-Use Disclosure Statement included (Step 18)",
    "Author names, affiliations, ORCID, funding and conflict-of-interest statements added",
    "Figures/tables at required resolution and referenced in order",
]
