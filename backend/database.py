"""SQLite persistence for projects, sections, sources, AI-use audit log, revisions and settings."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    question TEXT DEFAULT '', hypothesis TEXT DEFAULT '', outline TEXT DEFAULT '',
    target_csl TEXT DEFAULT 'apa', bib TEXT DEFAULT '',
    steps_done TEXT DEFAULT '[]', export_version TEXT DEFAULT '',
    created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL, ord INTEGER DEFAULT 0,
    content TEXT DEFAULT '', ai_baseline TEXT DEFAULT '',
    human_chars_changed INTEGER DEFAULT 0, ai_chars INTEGER DEFAULT 0,
    status TEXT DEFAULT '{}', updated REAL
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename TEXT, path TEXT, text TEXT DEFAULT '', tei TEXT DEFAULT '',
    parsed_sections TEXT DEFAULT '[]', parser TEXT DEFAULT '', indexed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ai_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER, section_id INTEGER, step INTEGER, tool TEXT, model TEXT,
    hardware_mode TEXT, prompt TEXT, words INTEGER, ts REAL
);
CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    engine TEXT, original TEXT, revised TEXT, hunks TEXT, notes TEXT DEFAULT '[]',
    status TEXT DEFAULT 'pending', ts REAL
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER, step INTEGER, payload TEXT, ts REAL
);
"""

SECTION_STATUS_KEYS = ("drafted", "edited", "grounded", "claims_fixed", "revised", "diffs_resolved")


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- generic helpers ----------
    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    # ---------- settings ----------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    # ---------- projects ----------
    def create_project(self, title: str, sections: list[str] | None = None) -> dict:
        now = time.time()
        pid = self.x("INSERT INTO projects(title,created,updated) VALUES(?,?,?)", (title, now, now))
        for i, name in enumerate(sections or ["Abstract", "Introduction", "Methods", "Results", "Discussion", "Conclusion"]):
            self.x("INSERT INTO sections(project_id,name,ord,status,updated) VALUES(?,?,?,?,?)",
                   (pid, name, i, json.dumps({}), now))
        return self.get_project(pid)

    def get_project(self, pid: int) -> dict | None:
        p = self.one("SELECT * FROM projects WHERE id=?", (pid,))
        if not p:
            return None
        p["steps_done"] = json.loads(p["steps_done"] or "[]")
        p["sections"] = self.list_sections(pid)
        p["source_count"] = self.one("SELECT COUNT(*) c FROM sources WHERE project_id=?", (pid,))["c"]
        return p

    def list_projects(self) -> list[dict]:
        return self.q("SELECT id,title,updated FROM projects ORDER BY updated DESC")

    def update_project(self, pid: int, **fields) -> dict:
        allowed = {"title", "question", "hypothesis", "outline", "target_csl", "bib", "export_version"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if sets:
            cols = ",".join(f"{k}=?" for k in sets)
            self.x(f"UPDATE projects SET {cols}, updated=? WHERE id=?", (*sets.values(), time.time(), pid))
        return self.get_project(pid)

    def mark_step(self, pid: int, step: int, done: bool = True) -> list[int]:
        p = self.one("SELECT steps_done FROM projects WHERE id=?", (pid,))
        steps = set(json.loads(p["steps_done"] or "[]"))
        steps.add(step) if done else steps.discard(step)
        self.x("UPDATE projects SET steps_done=?, updated=? WHERE id=?", (json.dumps(sorted(steps)), time.time(), pid))
        return sorted(steps)

    # ---------- sections ----------
    def list_sections(self, pid: int) -> list[dict]:
        rows = self.q("SELECT * FROM sections WHERE project_id=? ORDER BY ord, id", (pid,))
        for r in rows:
            r["status"] = json.loads(r["status"] or "{}")
            r["human_edit_ratio"] = self.edit_ratio(r)
        return rows

    def get_section(self, sid: int) -> dict | None:
        r = self.one("SELECT * FROM sections WHERE id=?", (sid,))
        if r:
            r["status"] = json.loads(r["status"] or "{}")
            r["human_edit_ratio"] = self.edit_ratio(r)
        return r

    @staticmethod
    def edit_ratio(r: dict) -> float:
        total = (r.get("ai_chars") or 0) + (r.get("human_chars_changed") or 0)
        if total == 0:
            return 1.0 if (r.get("content") or "").strip() else 0.0
        return round((r.get("human_chars_changed") or 0) / total, 3)

    def add_section(self, pid: int, name: str) -> dict:
        n = self.one("SELECT COALESCE(MAX(ord),-1)+1 n FROM sections WHERE project_id=?", (pid,))["n"]
        sid = self.x("INSERT INTO sections(project_id,name,ord,status,updated) VALUES(?,?,?,?,?)",
                     (pid, name, n, "{}", time.time()))
        return self.get_section(sid)

    def set_section_status(self, sid: int, **flags) -> dict:
        s = self.get_section(sid)
        st = s["status"]
        st.update({k: bool(v) for k, v in flags.items()})
        self.x("UPDATE sections SET status=?, updated=? WHERE id=?", (json.dumps(st), time.time(), sid))
        return self.get_section(sid)

    def save_section_content(self, sid: int, content: str, *, by_ai: bool = False) -> dict:
        import difflib
        s = self.get_section(sid)
        old = s["content"] or ""
        if by_ai:
            self.x("UPDATE sections SET content=?, ai_baseline=?, ai_chars=ai_chars+?, updated=? WHERE id=?",
                   (content, content, len(content), time.time(), sid))
        else:
            sm = difflib.SequenceMatcher(None, old, content, autojunk=False)
            changed = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")
            self.x("UPDATE sections SET content=?, human_chars_changed=human_chars_changed+?, updated=? WHERE id=?",
                   (content, changed, time.time(), sid))
        return self.get_section(sid)

    # ---------- sources ----------
    def add_source(self, pid: int, filename: str, path: str, text: str = "") -> dict:
        sid = self.x("INSERT INTO sources(project_id,filename,path,text) VALUES(?,?,?,?)", (pid, filename, path, text))
        return self.one("SELECT id,filename,parser,indexed FROM sources WHERE id=?", (sid,))

    def list_sources(self, pid: int, full: bool = False) -> list[dict]:
        cols = "*" if full else "id,filename,parser,indexed,length(text) chars"
        rows = self.q(f"SELECT {cols} FROM sources WHERE project_id=? ORDER BY id", (pid,))
        if full:
            for r in rows:
                r["parsed_sections"] = json.loads(r["parsed_sections"] or "[]")
        return rows

    def update_source(self, src_id: int, **fields) -> None:
        if "parsed_sections" in fields:
            fields["parsed_sections"] = json.dumps(fields["parsed_sections"])
        cols = ",".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE sources SET {cols} WHERE id=?", (*fields.values(), src_id))

    # ---------- AI log ----------
    def log_ai(self, *, project_id: int, section_id: int | None, step: int, tool: str, model: str,
               hardware_mode: str, prompt: str, words: int) -> int:
        return self.x("INSERT INTO ai_log(project_id,section_id,step,tool,model,hardware_mode,prompt,words,ts) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (project_id, section_id, step, tool, model, hardware_mode, prompt, words, time.time()))

    def ai_log(self, pid: int) -> list[dict]:
        return self.q("SELECT a.*, s.name section_name FROM ai_log a LEFT JOIN sections s ON s.id=a.section_id "
                      "WHERE a.project_id=? ORDER BY a.ts", (pid,))

    # ---------- revisions ----------
    def add_revision(self, section_id: int, engine: str, original: str, revised: str,
                     hunks: list, notes: list) -> dict:
        rid = self.x("INSERT INTO revisions(section_id,engine,original,revised,hunks,notes,ts) VALUES(?,?,?,?,?,?,?)",
                     (section_id, engine, original, revised, json.dumps(hunks), json.dumps(notes), time.time()))
        return self.get_revision(rid)

    def get_revision(self, rid: int) -> dict | None:
        r = self.one("SELECT * FROM revisions WHERE id=?", (rid,))
        if r:
            r["hunks"] = json.loads(r["hunks"])
            r["notes"] = json.loads(r["notes"])
        return r

    def set_revision_status(self, rid: int, status: str) -> None:
        self.x("UPDATE revisions SET status=? WHERE id=?", (status, rid))

    # ---------- findings ----------
    def save_finding(self, pid: int, step: int, payload: Any) -> int:
        return self.x("INSERT INTO findings(project_id,step,payload,ts) VALUES(?,?,?,?)",
                      (pid, step, json.dumps(payload), time.time()))

    def latest_finding(self, pid: int, step: int) -> Any:
        r = self.one("SELECT payload FROM findings WHERE project_id=? AND step=? ORDER BY id DESC LIMIT 1", (pid, step))
        return json.loads(r["payload"]) if r else None
