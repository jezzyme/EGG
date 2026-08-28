# -*- coding: utf-8 -*-
"""SQLite 저장소.

파일(JSON) 저장 방식은 워커가 여러 개인 환경에서 동시에 쓰면 기록이 유실될 수 있다.
SQLite는 프로세스 사이의 잠금을 데이터베이스가 직접 처리하므로 그 문제가 사라진다.

계정·연습 기록·로그인 시도·비밀번호 재설정 토큰을 한 파일에 담고,
가능한 환경에서는 파일 권한을 소유자 전용(600)으로 좁힌다.
"""

import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import time
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    email           TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    password_hash   TEXT NOT NULL DEFAULT '',
    provider        TEXT NOT NULL DEFAULT 'local',
    google_sub      TEXT,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id         TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_by_user ON runs (email, created_at);

CREATE TABLE IF NOT EXISTS login_attempts (
    key      TEXT PRIMARY KEY,
    count    INTEGER NOT NULL,
    last_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reset_tokens (
    token_hash TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0
);
"""

_path = None
_ready = False


def db_path():
    """데이터베이스 파일 위치를 정한다. 쓸 수 없으면 임시 폴더로 물러선다."""
    global _path
    if _path:
        return _path

    candidates = [
        os.environ.get("EGG_DB_PATH"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "egg.db"),
        os.path.join(tempfile.gettempdir(), "egg.db"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        folder = os.path.dirname(candidate) or "."
        try:
            os.makedirs(folder, exist_ok=True)
            with open(candidate, "a", encoding="utf-8"):
                pass
            _path = candidate
            return _path
        except OSError:
            continue

    _path = os.path.join(tempfile.gettempdir(), "egg.db")
    return _path


def _restrict_windows(path):
    """윈도우에서는 icacls 로 상속 권한을 끊고 현재 사용자에게만 권한을 준다."""
    account = os.environ.get("USERNAME")
    if not account:
        return
    try:
        subprocess.run(["icacls", path, "/inheritance:r",
                        "/grant:r", "%s:(F)" % account],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass                                                    # 권한 조정에 실패해도 동작은 계속한다


def _restrict_permissions(path):
    """데이터베이스 파일을 소유자만 읽고 쓸 수 있게 제한한다."""
    for suffix in ("", "-wal", "-shm"):
        target = path + suffix
        if not os.path.exists(target):
            continue
        try:
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)       # 0o600 (POSIX)
        except OSError:
            pass
        if os.name == "nt":
            _restrict_windows(target)


def connect():
    """연결을 새로 연다. 호출한 쪽에서 with 문으로 닫는다."""
    global _ready
    path = db_path()
    connection = sqlite3.connect(path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")       # 읽기와 쓰기가 서로를 막지 않게 한다
    connection.execute("PRAGMA busy_timeout=8000")      # 다른 프로세스가 쓰는 중이면 기다린다
    connection.execute("PRAGMA foreign_keys=ON")
    if not _ready:
        connection.executescript(SCHEMA)
        _add_missing_columns(connection)
        connection.commit()
        _restrict_permissions(path)
        _ready = True
    return connection


def _add_missing_columns(connection):
    """예전 버전으로 만든 데이터베이스에 새 컬럼을 채워 넣는다."""
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
    for column, definition in (("provider", "TEXT NOT NULL DEFAULT 'local'"),
                               ("google_sub", "TEXT")):
        if column not in existing:
            connection.execute("ALTER TABLE users ADD COLUMN %s %s" % (column, definition))


def query(sql, params=(), one=False):
    with connect() as connection:
        rows = connection.execute(sql, params).fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql, params=()):
    with connect() as connection:
        cursor = connection.execute(sql, params)
        connection.commit()
        return cursor.rowcount


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ========== 기존 JSON 데이터 이전 ==========
def migrate_json(users_file=None, data_folder=None):
    """예전 JSON 파일이 남아 있으면 한 번만 데이터베이스로 옮긴다."""
    import hashlib

    moved_users = moved_runs = 0
    base = os.path.dirname(os.path.abspath(__file__))
    users_file = users_file or os.environ.get("EGG_USER_STORE") or os.path.join(base, "users.json")
    data_folder = data_folder or os.environ.get("EGG_DATA_DIR") or os.path.join(base, "data")

    known_emails = {}
    if os.path.exists(users_file):
        try:
            with open(users_file, encoding="utf-8") as file:
                users = json.load(file)
        except (OSError, ValueError):
            users = {}
        for email, user in (users or {}).items():
            if not isinstance(user, dict):
                continue
            known_emails[hashlib.sha256(email.encode("utf-8")).hexdigest()] = email
            changed = execute(
                "INSERT OR IGNORE INTO users (email, name, password_hash, provider, session_version, created_at)"
                " VALUES (?, ?, ?, 'local', 1, ?)",
                (email, user.get("name", ""), user.get("password_hash", ""), now_iso()))
            moved_users += changed

    if os.path.isdir(data_folder):
        for filename in os.listdir(data_folder):
            if not filename.endswith(".json"):
                continue
            email = known_emails.get(filename[:-5])
            if not email:
                continue                                # 주인을 알 수 없는 파일은 건드리지 않는다
            try:
                with open(os.path.join(data_folder, filename), encoding="utf-8") as file:
                    runs = (json.load(file) or {}).get("runs") or []
            except (OSError, ValueError):
                continue
            for run in runs:
                if not isinstance(run, dict) or not run.get("id"):
                    continue
                moved_runs += execute(
                    "INSERT OR IGNORE INTO runs (id, email, created_at, updated_at, done, payload)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (run["id"], email, run.get("created_at") or now_iso(), now_iso(),
                     1 if run.get("done") else 0, json.dumps(run, ensure_ascii=False)))

    return moved_users, moved_runs


def cleanup_expired(now=None):
    """만료된 재설정 토큰과 오래된 로그인 시도 기록을 지운다."""
    moment = now if now is not None else time.time()
    execute("DELETE FROM reset_tokens WHERE expires_at < ? OR used = 1", (moment,))
    execute("DELETE FROM login_attempts WHERE last_at < ?", (moment - 86400,))
