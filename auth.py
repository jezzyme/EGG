# -*- coding: utf-8 -*-
"""계정 관리.

SQLite에 계정을 보관한다. 비밀번호는 해시로만 저장하고 원문은 남기지 않는다.
로그인 시도 제한과 비밀번호 재설정 토큰도 데이터베이스에 두기 때문에
서버를 다시 켜거나 워커가 여러 개여도 그대로 유지된다.
"""

import hashlib
import os
import re
import secrets
import time

from werkzeug.security import check_password_hash, generate_password_hash

import db

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
MIN_PASSWORD = 8

# 요청 종류별 제한: (허용 횟수, 제한 시간(초), 안내 문구)
RATE_RULES = {
    # 로그인 실패가 이만큼 쌓이면 5분 동안 잠근다
    "login": (8, 300, "로그인 시도가 너무 많습니다. %d초 뒤에 다시 시도해 주세요."),
    # 같은 곳에서 이메일을 대량으로 넣어 가입 여부를 캐내는 것을 막는다
    "signup": (12, 600, "가입 시도가 너무 많습니다. %d초 뒤에 다시 시도해 주세요."),
    # 자기소개서 파일 분석은 서버 자원을 쓰므로 횟수를 제한한다
    "upload": (30, 600, "파일 분석 요청이 너무 잦습니다. %d초 뒤에 다시 시도해 주세요."),
    # 비밀번호 재설정 메일 폭탄을 막는다
    "reset": (5, 3600, "비밀번호 재설정 요청이 너무 많습니다. %d분 뒤에 다시 시도해 주세요."),
}

MAX_ATTEMPTS = RATE_RULES["login"][0]
LOCKOUT_SECONDS = RATE_RULES["login"][1]

# 비밀번호 재설정 토큰
RESET_TTL = 1800            # 30분

# 존재하지 않는 계정에도 같은 비용의 해시 검증을 수행하기 위한 더미 값
_DUMMY_HASH = generate_password_hash("egg-dummy-password")


def normalize_email(email):
    return (email or "").strip().lower()


def validate(email, password, name=None, confirm=None):
    """가입·변경 입력값을 검사하고 첫 번째 오류 메시지를 돌려준다."""
    email = normalize_email(email)
    if not EMAIL_PATTERN.match(email):
        return "이메일 형식이 올바르지 않습니다."
    if name is not None and not (name or "").strip():
        return "이름을 입력해 주세요."
    if len(password or "") < MIN_PASSWORD:
        return "비밀번호는 %d자 이상으로 입력해 주세요." % MIN_PASSWORD
    if not re.search(r"[A-Za-z]", password or "") or not re.search(r"\d", password or ""):
        return "비밀번호는 영문과 숫자를 함께 포함해 주세요."
    if confirm is not None and password != confirm:
        return "비밀번호가 서로 일치하지 않습니다."
    return None


def _row_to_user(row):
    keys = row.keys()
    return {
        "email": row["email"],
        "name": row["name"],
        "session_version": row["session_version"],
        "provider": row["provider"] if "provider" in keys else "local",
        "has_password": bool(row["password_hash"]) if "password_hash" in keys else True,
    }


def find_user(email):
    row = db.query("SELECT * FROM users WHERE email = ?", (normalize_email(email),), one=True)
    return _row_to_user(row) if row else None


def create_user(email, password, name):
    """새 사용자를 만든다. 성공하면 (사용자, None), 실패하면 (None, 오류)."""
    email = normalize_email(email)
    error = validate(email, password, name)
    if error:
        return None, error

    if find_user(email):
        return None, "이미 가입된 이메일입니다. 로그인해 주세요."

    changed = db.execute(
        "INSERT OR IGNORE INTO users (email, name, password_hash, provider, session_version, created_at)"
        " VALUES (?, ?, ?, 'local', 1, ?)",
        (email, name.strip(), generate_password_hash(password), db.now_iso()))
    if not changed:                       # 동시에 같은 이메일로 가입한 경우
        return None, "이미 가입된 이메일입니다. 로그인해 주세요."
    return find_user(email), None


def verify_user(email, password):
    """로그인 확인. 성공하면 (사용자, None)."""
    email = normalize_email(email)
    row = db.query("SELECT * FROM users WHERE email = ?", (email,), one=True)
    if not row:
        # 가입되지 않은 이메일도 같은 시간이 걸리게 해, 응답 속도로 가입 여부를 알아내지 못하게 한다
        check_password_hash(_DUMMY_HASH, password or "")
        return None, "이메일 또는 비밀번호가 올바르지 않습니다."
    if not row["password_hash"]:
        # 구글로만 가입한 계정에는 비밀번호가 없다
        return None, "구글로 가입한 계정입니다. '구글로 계속하기'로 로그인해 주세요."
    if not check_password_hash(row["password_hash"], password or ""):
        return None, "이메일 또는 비밀번호가 올바르지 않습니다."
    return _row_to_user(row), None


def login_or_create_google_user(profile):
    """구글 계정으로 로그인한다.

    같은 이메일로 이미 가입한 계정이 있으면 그 계정에 구글 로그인을 연결한다.
    (구글이 이메일 소유를 확인해 주므로 안전하게 연결할 수 있다.)
    처음이라면 비밀번호 없는 계정을 새로 만든다.
    """
    email = normalize_email(profile.get("email"))
    if not EMAIL_PATTERN.match(email):
        return None, "구글 계정 이메일을 확인하지 못했습니다."

    name = (profile.get("name") or email.split("@")[0]).strip()[:40]
    sub = profile.get("sub") or ""
    row = db.query("SELECT * FROM users WHERE email = ?", (email,), one=True)

    if row:
        db.execute("UPDATE users SET google_sub = ?,"
                   " provider = CASE WHEN password_hash = '' THEN 'google' ELSE 'local+google' END"
                   " WHERE email = ?", (sub, email))
    else:
        db.execute("INSERT INTO users (email, name, password_hash, provider, google_sub,"
                   " session_version, created_at) VALUES (?, ?, '', 'google', ?, 1, ?)",
                   (email, name, sub, db.now_iso()))

    user = find_user(email)
    user["is_new"] = row is None          # 처음 가입한 계정이면 환영 화면을 보여 준다
    return user, None


def set_password(email, new_password, confirm):
    """비밀번호가 없는(구글) 계정에 비밀번호를 새로 만든다."""
    email = normalize_email(email)
    row = db.query("SELECT password_hash FROM users WHERE email = ?", (email,), one=True)
    if not row:
        return False, "계정을 찾을 수 없습니다."
    if row["password_hash"]:
        return False, "이미 비밀번호가 설정된 계정입니다."
    error = validate(email, new_password, None, confirm)
    if error:
        return False, error

    db.execute("UPDATE users SET password_hash = ?, provider = 'local+google',"
               " session_version = session_version + 1 WHERE email = ?",
               (generate_password_hash(new_password), email))
    return True, None


def delete_user(email):
    """계정과 연습 기록을 함께 삭제한다(회원 탈퇴)."""
    email = normalize_email(email)
    db.execute("DELETE FROM runs WHERE email = ?", (email,))
    db.execute("DELETE FROM reset_tokens WHERE email = ?", (email,))
    return db.execute("DELETE FROM users WHERE email = ?", (email,)) > 0


def session_version(email):
    """현재 유효한 세션 세대. 로그아웃·비밀번호 변경 시 올라간다."""
    row = db.query("SELECT session_version FROM users WHERE email = ?", (normalize_email(email),), one=True)
    return row["session_version"] if row else None


def bump_session_version(email):
    """기존에 발급된 로그인 쿠키를 모두 무효로 만든다."""
    db.execute("UPDATE users SET session_version = session_version + 1 WHERE email = ?",
               (normalize_email(email),))


def change_password(email, current_password, new_password, confirm):
    """비밀번호 변경. 성공하면 (True, None)이며 기존 로그인 쿠키는 모두 무효가 된다."""
    email = normalize_email(email)
    row = db.query("SELECT password_hash FROM users WHERE email = ?", (email,), one=True)
    if row is not None and not row["password_hash"]:
        return False, "구글로 가입한 계정입니다. 아래에서 비밀번호를 새로 설정해 주세요."

    _, error = verify_user(email, current_password)
    if error:
        return False, "현재 비밀번호가 올바르지 않습니다."
    error = validate(email, new_password, None, confirm)
    if error:
        return False, error
    if current_password == new_password:
        return False, "지금 쓰는 비밀번호와 다른 비밀번호로 바꿔 주세요."

    db.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE email = ?",
               (generate_password_hash(new_password), email))
    return True, None


# ========== 로그인·가입 시도 제한 ==========
def _attempt_key(kind, identifier, address):
    return "%s|%s|%s" % (kind, normalize_email(identifier), address or "-")


def _attempt_row(key):
    row = db.query("SELECT count, last_at FROM login_attempts WHERE key = ?", (key,), one=True)
    return (row["count"], row["last_at"]) if row else (0, 0.0)


def check_limit(kind, identifier, address):
    """제한에 걸렸으면 안내 문구를, 아니면 None을 돌려준다."""
    allowed, window, message = RATE_RULES[kind]
    key = _attempt_key(kind, identifier, address)
    count, last = _attempt_row(key)
    if count < allowed:
        return None

    remaining = window - (time.time() - last)
    if remaining <= 0:
        db.execute("DELETE FROM login_attempts WHERE key = ?", (key,))
        return None
    unit = int(remaining // 60) if "분" in message else int(remaining)
    return message % max(1, unit)


def record_attempt(kind, identifier, address):
    """시도 횟수를 하나 올린다(제한 시간이 지났으면 처음부터 다시 센다)."""
    window = RATE_RULES[kind][1]
    key = _attempt_key(kind, identifier, address)
    count, last = _attempt_row(key)
    if time.time() - last > window:
        count = 0
    now = time.time()
    db.execute("INSERT INTO login_attempts (key, count, last_at) VALUES (?, ?, ?)"
               " ON CONFLICT(key) DO UPDATE SET count = ?, last_at = ?",
               (key, count + 1, now, count + 1, now))


def clear_attempts(kind, identifier, address):
    db.execute("DELETE FROM login_attempts WHERE key = ?", (_attempt_key(kind, identifier, address),))


# --- 아래 이름들은 기존 호출부와의 호환을 위해 남겨 둔다 ---
def lockout_message(email, address):
    return check_limit("login", email, address)


def record_failure(email, address):
    record_attempt("login", email, address)


def clear_failures(email, address):
    clear_attempts("login", email, address)


def signup_throttled(address):
    return check_limit("signup", "", address)


def record_signup_attempt(address):
    record_attempt("signup", "", address)


# ========== 비밀번호 재설정 ==========
def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_reset_token(email):
    """재설정 토큰을 만든다. 계정이 없으면 None(호출한 쪽은 티를 내지 않는다).

    데이터베이스에는 토큰의 해시만 저장하므로, 파일을 열어 봐도 토큰을 알 수 없다.
    """
    email = normalize_email(email)
    if not find_user(email):
        return None
    db.execute("DELETE FROM reset_tokens WHERE email = ?", (email,))
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO reset_tokens (token_hash, email, expires_at, used) VALUES (?, ?, ?, 0)",
               (_hash_token(token), email, time.time() + RESET_TTL))
    return token


def email_for_token(token):
    if not token:
        return None
    row = db.query("SELECT email, expires_at, used FROM reset_tokens WHERE token_hash = ?",
                   (_hash_token(token),), one=True)
    if not row or row["used"] or row["expires_at"] < time.time():
        return None
    return row["email"]


def consume_reset_token(token, new_password, confirm):
    """토큰으로 비밀번호를 바꾼다. 성공하면 (True, None)."""
    email = email_for_token(token)
    if not email:
        return False, "재설정 링크가 만료되었거나 올바르지 않습니다. 다시 요청해 주세요."
    error = validate(email, new_password, None, confirm)
    if error:
        return False, error

    db.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE email = ?",
               (generate_password_hash(new_password), email))
    db.execute("UPDATE reset_tokens SET used = 1 WHERE token_hash = ?", (_hash_token(token),))
    clear_failures(email, None)
    return True, None


def user_count():
    row = db.query("SELECT COUNT(*) AS total FROM users", one=True)
    return row["total"] if row else 0
