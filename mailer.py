# -*- coding: utf-8 -*-
"""메일 발송(비밀번호 재설정용).

SMTP 정보가 환경변수에 있으면 실제로 메일을 보내고, 없으면 보내지 않는다.
설정이 없더라도 화면 안내는 똑같이 보여 주므로 가입 여부가 드러나지 않는다.

환경변수
  EGG_SMTP_HOST      메일 서버 주소 (예: smtp.gmail.com)
  EGG_SMTP_PORT      포트 (기본 587. 465면 SSL로 접속)
  EGG_SMTP_USER      로그인 계정
  EGG_SMTP_PASSWORD  로그인 비밀번호(앱 비밀번호)
  EGG_MAIL_FROM      보내는 사람 (기본: EGG_SMTP_USER)
  EGG_SMTP_SSL       1이면 처음부터 SSL로 접속
"""

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

TIMEOUT = 10


def _setting(name, default=""):
    return (os.environ.get(name) or default).strip()


def host():
    return _setting("EGG_SMTP_HOST")


def sender():
    return _setting("EGG_MAIL_FROM") or _setting("EGG_SMTP_USER")


def enabled():
    """메일을 보낼 준비가 되었는지."""
    return bool(host() and sender())


def send(to_address, subject, body):
    """메일 한 통을 보낸다. 성공하면 (True, None)."""
    if not enabled():
        return False, "메일 설정이 없습니다."

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("EGG 면접 연습", sender()))
    message["To"] = to_address
    message.set_content(body)

    port = int(_setting("EGG_SMTP_PORT", "587") or 587)
    user = _setting("EGG_SMTP_USER")
    password = _setting("EGG_SMTP_PASSWORD")
    use_ssl = _setting("EGG_SMTP_SSL").lower() in ("1", "true", "yes") or port == 465

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host(), port, timeout=TIMEOUT,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host(), port, timeout=TIMEOUT)
        with server:
            server.ehlo()
            if not use_ssl:
                try:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                except smtplib.SMTPException:
                    pass                            # TLS를 지원하지 않는 내부 서버도 있다
            if user and password:
                server.login(user, password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError, ValueError) as error:
        return False, "%s: %s" % (type(error).__name__, error)
    return True, None


def send_reset_link(to_address, link):
    """비밀번호 재설정 링크를 보낸다."""
    body = (
        "안녕하세요, EGG입니다.\n\n"
        "비밀번호를 다시 설정하려면 아래 주소를 열어 주세요.\n\n"
        "%s\n\n"
        "이 링크는 30분 뒤에 만료되며 한 번만 사용할 수 있습니다.\n"
        "본인이 요청하지 않았다면 이 메일을 무시하셔도 됩니다. 비밀번호는 그대로 유지됩니다.\n\n"
        "— EGG 면접 연습\n"
    ) % link
    return send(to_address, "[EGG] 비밀번호 재설정 안내", body)
