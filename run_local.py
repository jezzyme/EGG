# -*- coding: utf-8 -*-
"""로컬에서 EGG를 실행한다.  python run_local.py  →  http://127.0.0.1:5000"""
import os
import secrets

# 재시작해도 로그인이 풀리지 않도록 고정 키를 만들어 둔다(최초 1회 생성)
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".egg_secret")
if not os.path.exists(KEY_FILE):
    with open(KEY_FILE, "w", encoding="utf-8") as file:
        file.write(secrets.token_hex(32))
with open(KEY_FILE, encoding="utf-8") as file:
    os.environ.setdefault("EGG_SECRET_KEY", file.read().strip())

os.environ.setdefault("EGG_ENV", "development")   # 로컬은 http라 HTTPS 강제를 끈다

# 구글 로그인 키를 .env.local 에 적어 두면 자동으로 읽는다(없으면 이메일 로그인만 동작)
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.local")
if os.path.exists(ENV_FILE):
    with open(ENV_FILE, encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

from app_flask import app

if __name__ == "__main__":
    import db
    where = "외부 PostgreSQL" if db.USE_POSTGRES else "로컬 파일 %s" % db.db_path()
    print("\n  EGG 실행 중 →  http://127.0.0.1:5000")
    print("  데이터 저장: %s" % where)
    print("  상태 확인:   http://127.0.0.1:5000/healthz")
    print("  (종료: Ctrl+C)\n")
    # 코드를 고치면 자동으로 다시 불러온다(디버거는 켜지 않아 안전하다)
    app.run(host="127.0.0.1", port=5000, use_reloader=True)
