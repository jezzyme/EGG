# -*- coding: utf-8 -*-
"""데이터 저장소.

두 가지 방식을 함께 지원한다.
  · 로컬 개발: SQLite 파일 하나(설정 없이 그대로 동작)
  · 배포:      PostgreSQL (EGG_DATABASE_URL 또는 DATABASE_URL 이 있으면 자동 전환)

무료 호스팅은 디스크가 배포·재시작마다 초기화되므로 계정과 연습 기록은 외부 데이터베이스에
두어야 남는다. 나머지 코드(auth·storage)는 이 파일의 query()·execute() 만 쓰기 때문에
저장 방식이 바뀌어도 손댈 필요가 없다.
"""

import contextlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from datetime import datetime

DATABASE_URL = (os.environ.get("EGG_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

# 설정이 잘못됐을 때 오래 기다리지 않고 바로 알 수 있게 짧게 잡는다
CONNECT_TIMEOUT = int(os.environ.get("EGG_DB_TIMEOUT") or 8)

# 두 데이터베이스가 다르게 쓰는 자료형
TYPES = {
    "sqlite": {"real": "REAL", "text": "TEXT", "int": "INTEGER"},
    "postgres": {"real": "DOUBLE PRECISION", "text": "TEXT", "int": "INTEGER"},
}

TABLES = [
    """CREATE TABLE IF NOT EXISTS users (
        email           {text} PRIMARY KEY,
        name            {text} NOT NULL,
        password_hash   {text} NOT NULL DEFAULT '',
        provider        {text} NOT NULL DEFAULT 'local',
        google_sub      {text},
        session_version {int} NOT NULL DEFAULT 1,
        created_at      {text} NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        id         {text} PRIMARY KEY,
        email      {text} NOT NULL,
        created_at {text} NOT NULL,
        updated_at {text} NOT NULL,
        done       {int} NOT NULL DEFAULT 0,
        payload    {text} NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS runs_by_user ON runs (email, created_at)",
    """CREATE TABLE IF NOT EXISTS login_attempts (
        key      {text} PRIMARY KEY,
        count    {int} NOT NULL,
        last_at  {real} NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS reset_tokens (
        token_hash {text} PRIMARY KEY,
        email      {text} NOT NULL,
        expires_at {real} NOT NULL,
        used       {int} NOT NULL DEFAULT 0
    )""",
]

# 나중에 추가된 컬럼(예전 데이터베이스를 그대로 쓰는 경우)
EXTRA_COLUMNS = [
    ("users", "provider", "TEXT NOT NULL DEFAULT 'local'"),
    ("users", "google_sub", "TEXT"),
]

# 표를 만들 때 쓰는 잠금(같은 프로세스 안 / 데이터베이스 전체)
_schema_lock = threading.Lock()
SCHEMA_LOCK_ID = 8175243                        # 이 앱만 쓰는 임의의 번호

_path = None
_schema_ready = False
_wal_ready = False
_pool = None
_pool_tried = False


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def backend():
    return "postgres" if USE_POSTGRES else "sqlite"


def db_path():
    """SQLite 파일 위치. Postgres 를 쓰는 중이면 그 사실을 알려 준다."""
    if USE_POSTGRES:
        return "PostgreSQL (외부 데이터베이스)"

    global _path
    if _path:
        return _path

    for candidate in (os.environ.get("EGG_DB_PATH"),
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "egg.db"),
                      os.path.join(tempfile.gettempdir(), "egg.db")):
        if not candidate:
            continue
        try:
            os.makedirs(os.path.dirname(candidate) or ".", exist_ok=True)
            with open(candidate, "a", encoding="utf-8"):
                pass
            _path = candidate
            return _path
        except OSError:
            continue

    _path = os.path.join(tempfile.gettempdir(), "egg.db")
    return _path


# ========== 질의 문법 변환 ==========
def translate(sql):
    """SQLite 문법으로 적은 질의를 현재 데이터베이스에 맞게 바꾼다.

    바꾸는 것은 두 가지뿐이다.
      · INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
      · 자리표시자 ? → %s
    """
    if not USE_POSTGRES:
        return sql

    statement = sql.strip()
    if statement.upper().startswith("INSERT OR IGNORE"):
        statement = re.sub(r"^INSERT\s+OR\s+IGNORE", "INSERT", statement, flags=re.I)
        statement = statement.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return statement.replace("?", "%s")


# ========== 연결 ==========
def _get_pool():
    """연결을 재사용한다(외부 데이터베이스는 접속 비용이 크다)."""
    global _pool, _pool_tried
    if _pool_tried:
        return _pool
    _pool_tried = True
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, timeout=CONNECT_TIMEOUT,
                               kwargs={"row_factory": dict_row}, open=True)
    except Exception:
        _pool = None                            # 풀을 쓸 수 없으면 매번 새로 연결한다
    return _pool


@contextlib.contextmanager
def connect():
    """연결을 열고, 블록이 끝나면 정리한다."""
    if USE_POSTGRES:
        import psycopg
        from psycopg.rows import dict_row

        pool = _get_pool()
        if pool is not None:
            with pool.connection() as connection:
                _ensure_schema(connection)
                yield connection
        else:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row,
                                 connect_timeout=CONNECT_TIMEOUT) as connection:
                _ensure_schema(connection)
                yield connection
        return

    connection = sqlite3.connect(db_path(), timeout=15.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=15000")    # 다른 쪽이 쓰는 중이면 기다린다
        connection.execute("PRAGMA foreign_keys=ON")
        _enable_wal(connection)
        _ensure_schema(connection)
        yield connection
    finally:
        connection.close()


def _enable_wal(connection):
    """읽기와 쓰기가 서로를 막지 않도록 WAL 모드로 바꾼다.

    이 설정은 데이터베이스 파일에 저장되므로 한 번만 하면 된다.
    연결마다 실행하면 다른 연결이 열려 있을 때 잠금 오류가 난다.
    """
    global _wal_ready
    if _wal_ready:
        return
    try:
        connection.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass                                    # 이미 켜져 있거나 다른 연결이 쓰는 중이면 넘어간다
    _wal_ready = True


def _run_ddl(connection, statement):
    """표 만들기 같은 문장을 하나씩, 각각 따로 처리한다.

    PostgreSQL 은 트랜잭션 안에서 오류가 한 번 나면 그 연결로 더는 아무것도 못 한다.
    그래서 실패하면 곧바로 되돌려(rollback) 연결을 계속 쓸 수 있게 한다.
    """
    try:
        connection.cursor().execute(statement)
        connection.commit()
        return True
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        return False                            # 이미 있거나 다른 워커가 먼저 만든 경우


def _ensure_schema(connection):
    """표를 만들고 빠진 컬럼을 채운다(최초 한 번).

    배포 직후에는 여러 워커가 동시에 시작하므로, 같은 표를 동시에 만들려다 충돌할 수 있다.
    프로세스 안에서는 잠금으로, 프로세스 사이에서는 데이터베이스 잠금으로 한 번에 하나만 하게 한다.
    """
    global _schema_ready
    if _schema_ready:
        return

    with _schema_lock:
        if _schema_ready:
            return

        locked = False
        if USE_POSTGRES:                        # 다른 워커가 끝낼 때까지 기다린다
            locked = _run_ddl(connection, "SELECT pg_advisory_lock(%d)" % SCHEMA_LOCK_ID)

        try:
            types = TYPES[backend()]
            for statement in TABLES:
                _run_ddl(connection, statement.format(**types))

            for table, column, definition in EXTRA_COLUMNS:
                try:
                    existing = _columns(connection, table)
                except Exception:
                    connection.rollback()
                    continue
                if column not in existing:
                    _run_ddl(connection, "ALTER TABLE %s ADD COLUMN %s %s"
                             % (table, column, definition))
        finally:
            if locked:
                _run_ddl(connection, "SELECT pg_advisory_unlock(%d)" % SCHEMA_LOCK_ID)

        if not USE_POSTGRES:
            _restrict_permissions(db_path())
        _schema_ready = True


def _columns(connection, table):
    """표에 어떤 컬럼이 있는지 확인한다."""
    cursor = connection.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                       (table,))
        return {row["column_name"] for row in cursor.fetchall()}
    cursor.execute("PRAGMA table_info(%s)" % table)
    return {row["name"] for row in cursor.fetchall()}


# ========== 읽기·쓰기 ==========
def query(sql, params=(), one=False):
    with connect() as connection:
        cursor = connection.cursor()
        cursor.execute(translate(sql), params)
        rows = cursor.fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql, params=()):
    with connect() as connection:
        cursor = connection.cursor()
        cursor.execute(translate(sql), params)
        connection.commit()
        return cursor.rowcount


# ========== 파일 권한(SQLite 전용) ==========
def _restrict_windows(path):
    """윈도우에서는 icacls 로 상속 권한을 끊고 현재 사용자에게만 권한을 준다."""
    account = os.environ.get("USERNAME")
    if not account:
        return
    try:
        subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", "%s:(F)" % account],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def _restrict_permissions(path):
    """데이터베이스 파일을 소유자만 읽고 쓸 수 있게 제한한다."""
    for suffix in ("", "-wal", "-shm"):
        target = path + suffix
        if not os.path.exists(target):
            continue
        try:
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)      # 0o600 (POSIX)
        except OSError:
            pass
        if os.name == "nt":
            _restrict_windows(target)


# ========== 예전 JSON 데이터 이전 ==========
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
            moved_users += execute(
                "INSERT OR IGNORE INTO users (email, name, password_hash, provider, session_version, created_at)"
                " VALUES (?, ?, ?, 'local', 1, ?)",
                (email, user.get("name", ""), user.get("password_hash", ""), now_iso()))

    if os.path.isdir(data_folder):
        for filename in os.listdir(data_folder):
            if not filename.endswith(".json"):
                continue
            email = known_emails.get(filename[:-5])
            if not email:
                continue                        # 주인을 알 수 없는 파일은 건드리지 않는다
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


def health():
    """데이터베이스에 연결되는지 확인한다(배포 점검용)."""
    try:
        query("SELECT 1")
    except Exception as error:
        return {"ok": False, "backend": backend(), "error": "%s: %s" % (type(error).__name__, error)}
    return {"ok": True, "backend": backend(), "location": db_path()}


def cleanup_expired(now=None):
    """만료된 재설정 토큰과 오래된 로그인 시도 기록을 지운다."""
    moment = now if now is not None else time.time()
    execute("DELETE FROM reset_tokens WHERE expires_at < ? OR used = 1", (moment,))
    execute("DELETE FROM login_attempts WHERE last_at < ?", (moment - 86400,))
