# -*- coding: utf-8 -*-
"""연습 기록 저장소.

답변 내용·자기소개서·평가 결과는 브라우저 쿠키에 담지 않고 SQLite에만 보관한다.
쿠키에는 로그인 정보와 진행 중인 연습 번호만 남는다.

여러 워커가 동시에 저장해도 데이터베이스가 잠금을 처리하므로 기록이 유실되지 않는다.
"""

import json
import secrets
from datetime import datetime

import db

MAX_RUNS = 30                 # 사용자당 보관하는 연습 기록 수


def _decode(row):
    try:
        run = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    return run if isinstance(run, dict) else None


def new_run(profile, questions):
    """새 연습 기록을 만든다(아직 저장하지는 않는다)."""
    now = datetime.now()
    return {
        "id": secrets.token_hex(8),
        "created_at": now.isoformat(timespec="seconds"),
        "created_label": now.strftime("%Y.%m.%d %H:%M"),
        "profile": profile,
        "questions": questions,
        "index": 0,
        "answers": [],
        "summary": None,
        "done": False,
    }


def save_run(email, run):
    """연습 기록을 저장한다. 같은 id가 있으면 덮어쓴다."""
    payload = json.dumps(run, ensure_ascii=False)
    db.execute(
        "INSERT INTO runs (id, email, created_at, updated_at, done, payload) VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at,"
        " done = excluded.done, payload = excluded.payload WHERE runs.email = excluded.email",
        (run["id"], email, run.get("created_at") or db.now_iso(), db.now_iso(),
         1 if run.get("done") else 0, payload))

    # 보관 개수를 넘으면 오래된 기록부터 지운다
    db.execute(
        "DELETE FROM runs WHERE email = ? AND id NOT IN ("
        "  SELECT id FROM runs WHERE email = ? ORDER BY created_at DESC, id DESC LIMIT ?)",
        (email, email, MAX_RUNS))
    return run["id"]


def get_run(email, run_id):
    """본인 기록만 돌려준다(다른 사용자의 id로는 절대 열리지 않는다)."""
    if not email or not run_id:
        return None
    row = db.query("SELECT payload FROM runs WHERE id = ? AND email = ?", (run_id, email), one=True)
    return _decode(row) if row else None


def latest_run(email, done=None):
    if not email:
        return None
    if done is None:
        row = db.query("SELECT payload FROM runs WHERE email = ?"
                       " ORDER BY created_at DESC, id DESC LIMIT 1", (email,), one=True)
    else:
        row = db.query("SELECT payload FROM runs WHERE email = ? AND done = ?"
                       " ORDER BY created_at DESC, id DESC LIMIT 1",
                       (email, 1 if done else 0), one=True)
    return _decode(row) if row else None


def list_runs(email):
    """마이페이지용 목록. 최근 기록이 앞에 온다."""
    if not email:
        return []
    rows = db.query("SELECT payload FROM runs WHERE email = ? ORDER BY created_at DESC, id DESC",
                    (email,))
    return [run for run in (_decode(row) for row in rows) if run]


def delete_run(email, run_id):
    return db.execute("DELETE FROM runs WHERE id = ? AND email = ?", (run_id, email)) > 0


def delete_all(email):
    """사용자의 연습 기록을 모두 지운다."""
    return db.execute("DELETE FROM runs WHERE email = ?", (email,)) > 0


def summarize_run(run):
    """목록에 보여 줄 요약 정보를 만든다."""
    summary = run.get("summary") or {}
    answers = run.get("answers") or []
    profile = run.get("profile") or {}
    return {
        "id": run.get("id"),
        "created_label": run.get("created_label"),
        "company": profile.get("company") or "지원 기업",
        "role": profile.get("role") or "지원 직무",
        "answer_count": len(answers),
        "question_count": len(run.get("questions") or []),
        "done": bool(run.get("done")),
        "total": summary.get("total"),
        "grade": summary.get("grade"),
        "pass_rate": summary.get("pass_rate"),
        "voice_count": summary.get("voice_count", 0),
        "text_count": summary.get("text_count", 0),
    }


def data_dir():
    """마이페이지 안내용. 실제 보관 위치를 알려 준다."""
    return db.db_path()
