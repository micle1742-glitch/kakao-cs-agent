"""재시작 대비 상태 저장 (sqlite). 사용자(botUserKey)별 대화 기록, 도구 호출 기록, 케이스 정보.

- 대화 기록: 사진·음성 bytes와 도구 호출 trace(Gemini Content)까지 저장해 재시작 뒤에도 같은 맥락으로 이어진다.
- 새 케이스가 오면 기록을 지우지 않고 active=0으로 내려 둔다 (지난 상담 확인용). 불러올 때는 active=1만.
- 도구 호출 기록은 API 이름·인자·결과를 남기는 감사용이며 LLM에는 대화 기록의 trace로 전달된다.
- 파일 위치: DATA_DIR(기본: 프로젝트 폴더)/state.db
"""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from google.genai import types

DATA_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).resolve().parent.parent)
DB_PATH = DATA_DIR / "state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    user_key   TEXT PRIMARY KEY,
    case_json  TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key   TEXT NOT NULL,
    tc_id      TEXT,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    trace_json TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_user ON messages (user_key, active, id);
CREATE TABLE IF NOT EXISTS media (
    message_id INTEGER NOT NULL,
    mime       TEXT NOT NULL,
    data       BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS media_message ON media (message_id);
CREATE TABLE IF NOT EXISTS submissions (
    submit_key  TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key    TEXT,
    tc_id       TEXT,
    name        TEXT NOT NULL,
    args_json   TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

_conn = None


def _db():
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # 이벤트 루프 한 스레드에서만 쓰고, 쓰기가 짧아 동기 호출로 충분
        _conn = sqlite3.connect(DB_PATH, isolation_level=None)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
    return _conn


def _now():
    return datetime.now().isoformat(timespec="seconds")


def load_case(user_key):
    row = _db().execute("SELECT case_json FROM cases WHERE user_key = ?", (user_key,)).fetchone()
    return json.loads(row[0]) if row else None


def start_case(user_key, case_info):
    """새 케이스: 케이스 정보를 바꾸고 이전 대화 기록을 내린다."""
    db = _db()
    db.execute("BEGIN")
    db.execute("INSERT OR REPLACE INTO cases VALUES (?, ?, ?)",
               (user_key, json.dumps(case_info, ensure_ascii=False), _now()))
    db.execute("UPDATE messages SET active = 0 WHERE user_key = ?", (user_key,))
    db.execute("COMMIT")


def load_history(user_key, limit):
    """최근 limit개 메시지 (오래된 것부터). assistant부터 시작하지 않도록 맞춘다."""
    db = _db()
    rows = db.execute(
        "SELECT id, role, content, trace_json FROM messages WHERE user_key = ? AND active = 1 "
        "ORDER BY id DESC LIMIT ?", (user_key, limit)).fetchall()[::-1]
    while rows and rows[0][1] != "user":
        rows.pop(0)
    history = []
    for message_id, role, content, trace_json in rows:
        message = {"role": role, "content": content}
        media = db.execute("SELECT mime, data FROM media WHERE message_id = ?", (message_id,)).fetchall()
        if media:
            message["media"] = [{"mime": mime, "data": data} for mime, data in media]
        if trace_json:
            message["trace"] = [types.Content.model_validate_json(c) for c in json.loads(trace_json)]
        history.append(message)
    return history


def append_turn(user_key, tc_id, user_message, assistant_message):
    """사용자 메시지와 답을 한 쌍으로 저장."""
    db = _db()
    db.execute("BEGIN")
    for m in (user_message, assistant_message):
        trace = m.get("trace")
        cur = db.execute(
            "INSERT INTO messages (user_key, tc_id, role, content, trace_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_key, tc_id, m["role"], m["content"],
             json.dumps([c.model_dump_json(exclude_none=True) for c in trace]) if trace else None, _now()))
        for x in m.get("media", ()):
            db.execute("INSERT INTO media VALUES (?, ?, ?)", (cur.lastrowid, x["mime"], x["data"]))
    db.execute("COMMIT")


def load_submission(submit_key):
    """같은 TC에서 이미 성공한 같은 접수의 결과 (중복 접수 방지, 재시작 뒤에도 유지)."""
    row = _db().execute("SELECT result_json FROM submissions WHERE submit_key = ?", (submit_key,)).fetchone()
    return json.loads(row[0]) if row else None


def save_submission(submit_key, result):
    _db().execute("INSERT OR REPLACE INTO submissions VALUES (?, ?, ?)",
                  (submit_key, json.dumps(result, ensure_ascii=False), _now()))


def clear_submissions():
    """연습 데이터 초기화(reset.py) 뒤 호출 — 서버가 비었으니 중복 방지 기록도 비운다."""
    _db().execute("DELETE FROM submissions")


def record_tool_call(user_key, tc_id, name, args, result):
    _db().execute(
        "INSERT INTO tool_calls (user_key, tc_id, name, args_json, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_key, tc_id, name, json.dumps(args, ensure_ascii=False), json.dumps(result, ensure_ascii=False), _now()))
