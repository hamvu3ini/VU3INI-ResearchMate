"""Built-in offline grammar/spelling rules used when Harper or LanguageTool binaries are not available.

Fast tier (Harper fallback): mechanical errors.  Deep tier (LanguageTool fallback): adds contextual rules.
"""
from __future__ import annotations

import re

MISSPELLINGS = {
    "teh": "the", "recieve": "receive", "recieved": "received", "seperate": "separate", "occured": "occurred",
    "occurence": "occurrence", "acheive": "achieve", "acheived": "achieved", "accomodate": "accommodate",
    "untill": "until", "wich": "which", "thier": "their", "definately": "definitely", "enviroment": "environment",
    "goverment": "government", "existance": "existence", "independant": "independent", "maintainance": "maintenance",
    "neccessary": "necessary", "necesary": "necessary", "occassion": "occasion", "persistant": "persistent",
    "prefered": "preferred", "reccomend": "recommend", "refered": "referred", "relevent": "relevant",
    "succesful": "successful", "sucessful": "successful", "tommorow": "tomorrow", "truely": "truly",
    "analysys": "analysis", "anaylsis": "analysis", "experiement": "experiment", "experimants": "experiments",
    "hypotheses's": "hypotheses'", "paramter": "parameter", "parametres": "parameters", "algoritm": "algorithm",
    "accross": "across", "arguement": "argument", "begining": "beginning", "beleive": "believe",
    "calender": "calendar", "commited": "committed", "concious": "conscious", "consistant": "consistent",
    "dependant": "dependent", "embarass": "embarrass", "foriegn": "foreign", "garantee": "guarantee",
    "humourous": "humorous", "immediatly": "immediately", "lenght": "length", "liason": "liaison",
    "millenium": "millennium", "noticable": "noticeable", "posession": "possession", "publically": "publicly",
    "refrence": "reference", "refernce": "reference", "resaerch": "research", "reserach": "research",
    "signficant": "significant", "significent": "significant", "statisticaly": "statistically", "thresold": "threshold",
    "wierd": "weird", "withold": "withhold", "methodolgy": "methodology", "litterature": "literature",
}

VOWEL_SOUND_EXCEPTIONS_AN = {"hour", "honest", "honour", "honor", "heir", "mri", "fmri", "sql", "ml", "llm", "nlp",
                             "hmm", "x-ray", "f1", "fbi", "rna", "mrna", "ssd", "led", "hdmi", "hpc"}
CONSONANT_SOUND_EXCEPTIONS_A = {"university", "unique", "unit", "use", "user", "usual", "european", "one", "once",
                                "uniform", "union", "utility", "unanimous", "universal", "useful", "euler", "ubiquitous",
                                "usb", "uri", "url", "unified"}

CONTEXT_RULES = [
    (r"\bless(?= (?:people|participants|samples|items|studies|papers|errors|cases|patients|students)\b)",
     "Use 'fewer' with countable nouns.", "fewer"),
    (r"\bcomprised of\b", "'Comprised of' is widely considered incorrect; use 'composed of' or 'comprises'.", "composed of"),
    (r"\b(could|should|would|must|might) of\b", "Use 'have' after modal verbs.", r"\1 have"),
    (r"\bthe data is\b", "In formal academic writing 'data' is often plural: 'the data are'.", "the data are"),
    (r"\bin regards to\b", "Use 'regarding' or 'with regard to'.", "regarding"),
    (r"\birregardless\b", "Use 'regardless'.", "regardless"),
    (r"\bits(?=\s+(?:been|a|an|the|not|important|clear|possible|likely|necessary)\b)", "Did you mean \"it's\" (it is/has)?", "it's"),
    (r"\bit's(?=\s+(?:own|performance|accuracy|results|value|parameters|weights|output)\b)", "Did you mean possessive 'its'?", "its"),
    (r"\b(affect)s? on\b", "Did you mean the noun 'effect on'?", "effect on"),
    (r"\bthen(?= (?:the baseline|previous|expected|before|others)\b)", "Comparison needs 'than'.", "than"),
    (r"\bbased off(?: of)?\b", "Use 'based on'.", "based on"),
    (r"\bdifferent than\b", "Formal style prefers 'different from'.", "different from"),
    (r"\bvery unique\b", "'Unique' is absolute; drop 'very'.", "unique"),
    (r"\b(\w+ly)-(\w+ed)\b", "Do not hyphenate an -ly adverb with a participle.", r"\1 \2"),
]


def _issue(rule: str, start: int, end: int, message: str, replacements=(), severity="warning", category="Grammar"):
    return {"rule": rule, "start": start, "end": end, "message": message,
            "replacements": [r for r in replacements if r is not None][:3], "severity": severity, "category": category}


def fast_check(text: str) -> list[dict]:
    issues = []
    for m in re.finditer(r"\b(\w+)\s+\1\b", text, re.I):
        if m.group(1).lower() not in ("that", "had"):
            issues.append(_issue("REPEATED_WORD", m.start(), m.end(), f"Repeated word “{m.group(1)}”.",
                                 [m.group(1)], "error", "Typo"))
    for m in re.finditer(r"[A-Za-z]+", text):
        w = m.group(0)
        fix = MISSPELLINGS.get(w.lower())
        if fix:
            fix = fix.capitalize() if w[0].isupper() else fix
            issues.append(_issue("SPELLING", m.start(), m.end(), f"Possible spelling mistake: “{w}”.", [fix],
                                 "error", "Spelling"))
    for m in re.finditer(r"\b([Aa]n?)\s+([A-Za-z][\w\-]*)", text):
        art, nxt = m.group(1), m.group(2)
        low = nxt.lower()
        starts_vowel = low[0] in "aeiou"
        needs_an = (starts_vowel and low not in CONSONANT_SOUND_EXCEPTIONS_A and not low.startswith(("uni", "eu", "one", "use"))) \
            or low in VOWEL_SOUND_EXCEPTIONS_AN or (nxt.isupper() and len(nxt) <= 5 and nxt[0] in "AEFHILMNORSX")
        if art.lower() == "a" and needs_an:
            issues.append(_issue("A_VS_AN", m.start(1), m.end(1), f"Use “an” before “{nxt}”.",
                                 ["An" if art[0].isupper() else "an"], "error"))
        elif art.lower() == "an" and not needs_an:
            issues.append(_issue("A_VS_AN", m.start(1), m.end(1), f"Use “a” before “{nxt}”.",
                                 ["A" if art[0].isupper() else "a"], "error"))
    for m in re.finditer(r"(?<=\S)  +(?=\S)", text):
        issues.append(_issue("DOUBLE_SPACE", m.start(), m.end(), "Multiple consecutive spaces.", [" "], "info",
                             "Whitespace"))
    for m in re.finditer(r"[,;:](?=[A-Za-z])", text):
        if not re.match(r"https?:|\d", text[max(0, m.start() - 6):m.start() + 3]):
            issues.append(_issue("MISSING_SPACE", m.start(), m.end(), "Add a space after punctuation.",
                                 [m.group(0) + " "], "warning", "Punctuation"))
    for m in re.finditer(r"\s+([,.;:!?])", text):
        issues.append(_issue("SPACE_BEFORE_PUNCT", m.start(), m.end(), "Remove the space before punctuation.",
                             [m.group(1)], "warning", "Punctuation"))
    for m in re.finditer(r"(?:^|[.!?]\s+)([a-z][a-z]+)", text):
        word = m.group(1)
        if word not in ("e", "i", "et", "vs", "al", "etc"):
            issues.append(_issue("SENTENCE_CASE", m.start(1), m.start(1) + 1,
                                 "Sentence should start with a capital letter.", [word[0].upper()], "warning",
                                 "Capitalisation"))
    return _dedupe(issues)


def deep_check(text: str) -> list[dict]:
    issues = fast_check(text)
    for pat, msg, repl in CONTEXT_RULES:
        for m in re.finditer(pat, text, re.I):
            try:
                rep = m.expand(repl)
            except re.error:
                rep = repl
            issues.append(_issue("CONTEXT_" + re.sub(r"\W+", "_", msg[:18]).upper().strip("_"),
                                 m.start(), m.end(), msg, [rep], "warning", "Grammar"))
    for m in re.finditer(r"\b(was|were|is|are|been|being|be)\s+(\w+ed)\s+by\b", text, re.I):
        issues.append(_issue("PASSIVE_VOICE", m.start(), m.end(),
                             "Passive voice — consider an active construction if the agent matters.", [],
                             "info", "Style"))
    for m in re.finditer(r"[^.!?\n]{260,}[.!?]", text):
        issues.append(_issue("LONG_SENTENCE", m.start(), m.end(),
                             "Very long sentence (>260 characters); consider splitting.", [], "info", "Style"))
    return _dedupe(issues)


def _dedupe(issues: list[dict]) -> list[dict]:
    seen, out = set(), []
    for i in sorted(issues, key=lambda x: (x["start"], x["end"])):
        k = (i["start"], i["end"], i["rule"])
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out
