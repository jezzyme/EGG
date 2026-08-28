# -*- coding: utf-8 -*-
"""구글 간편 로그인(OAuth 2.0 / OpenID Connect).

표준 Authorization Code 흐름을 쓴다.
  1) 사용자를 구글 동의 화면으로 보낸다(state로 위조 요청을 막는다).
  2) 구글이 돌려준 code를 서버가 직접 토큰으로 바꾼다(브라우저를 거치지 않는다).
  3) 그 토큰으로 구글에 사용자 정보를 물어본다(HTTPS 직접 통신이라 신뢰할 수 있다).

외부 라이브러리 없이 표준 라이브러리만 사용한다.
환경변수 GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET 가 없으면 기능이 꺼진 상태로 동작한다.
"""

import json
import os
import secrets
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

SCOPES = "openid email profile"
TIMEOUT = 10


def client_id():
    return (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()


def client_secret():
    return (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()


def enabled():
    """구글 로그인 사용 준비가 되었는지."""
    return bool(client_id() and client_secret())


def make_state():
    return secrets.token_urlsafe(24)


def authorize_url(redirect_uri, state, login_hint=None):
    """구글 동의 화면 주소를 만든다."""
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return AUTH_ENDPOINT + "?" + urlencode(params)


def _post(url, data):
    request = Request(url, data=urlencode(data).encode("utf-8"),
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Accept": "application/json"})
    with urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _get(url, token):
    request = Request(url, headers={"Authorization": "Bearer " + token,
                                    "Accept": "application/json"})
    with urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def exchange_code(code, redirect_uri):
    """인가 코드를 액세스 토큰으로 바꾼다. 성공하면 (토큰정보, None)."""
    if not enabled():
        return None, "구글 로그인이 설정되지 않았습니다."
    try:
        token = _post(TOKEN_ENDPOINT, {
            "code": code,
            "client_id": client_id(),
            "client_secret": client_secret(),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
    except HTTPError as error:
        return None, "구글 인증에 실패했습니다. (%s)" % error.code
    except (URLError, ValueError, TimeoutError):
        return None, "구글 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."

    if not token.get("access_token"):
        return None, "구글에서 인증 정보를 받지 못했습니다."
    return token, None


def fetch_profile(access_token):
    """액세스 토큰으로 구글 계정 정보를 가져온다. 성공하면 (프로필, None)."""
    try:
        info = _get(USERINFO_ENDPOINT, access_token)
    except HTTPError as error:
        return None, "구글 계정 정보를 불러오지 못했습니다. (%s)" % error.code
    except (URLError, ValueError, TimeoutError):
        return None, "구글 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."

    email = (info.get("email") or "").strip().lower()
    if not email:
        return None, "구글 계정에서 이메일을 확인하지 못했습니다."
    if info.get("email_verified") is False:
        # 구글에서 이메일 소유가 확인되지 않은 계정은 기존 계정과 연결하면 위험하다
        return None, "이메일이 확인되지 않은 구글 계정입니다."

    name = (info.get("name") or info.get("given_name") or email.split("@")[0]).strip()
    return {"email": email, "name": name[:40], "sub": info.get("sub") or ""}, None


def login_with_code(code, redirect_uri):
    """코드 교환부터 프로필 조회까지 한 번에 처리한다."""
    token, error = exchange_code(code, redirect_uri)
    if error:
        return None, error
    return fetch_profile(token["access_token"])
