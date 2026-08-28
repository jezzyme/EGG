import hmac
import json
import random
import re
import html
import os
import tempfile
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, abort, render_template, request, session, redirect, url_for, jsonify
import secrets

from werkzeug.middleware.proxy_fix import ProxyFix

import auth
import db
import documents
import forms
import google_login
import mailer
import scoring
import storage

# EGG_ENV=production 이면 HTTPS 전용으로 동작한다(쿠키 Secure, HTTP 접속 시 HTTPS로 이동, HSTS).
PRODUCTION = os.environ.get("EGG_ENV", "").lower() in ("production", "prod", "live")


def flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


FORCE_HTTPS = flag("EGG_FORCE_HTTPS", PRODUCTION)

# 메일 링크·구글 콜백처럼 밖으로 나가는 주소는 여기 적힌 값으로만 만든다.
# 설정하지 않으면 요청의 Host 를 쓰므로, 운영에서는 반드시 지정한다.
BASE_URL = (os.environ.get("EGG_BASE_URL") or "").rstrip("/")

# 허용할 접속 주소(쉼표로 구분). 설정하면 다른 Host 로 온 요청을 거절한다.
ALLOWED_HOSTS = [name.strip().lower() for name in
                 (os.environ.get("EGG_ALLOWED_HOSTS") or "").split(",") if name.strip()]

app = Flask(__name__)
# 리버스 프록시 뒤에서 실제 접속 규약(https)을 읽는다.
# X-Forwarded-Host 는 아무나 보낼 수 있어 기본으로 신뢰하지 않는다.
# (프록시가 이 값을 직접 덮어써 준다고 확신할 때만 EGG_TRUST_PROXY_HOST=1)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1,
                        x_host=1 if flag("EGG_TRUST_PROXY_HOST", False) else 0)


def external_url(endpoint, **values):
    """밖으로 나가는 전체 주소를 만든다(요청 헤더를 그대로 믿지 않는다)."""
    if BASE_URL:
        return BASE_URL + url_for(endpoint, **values)
    return url_for(endpoint, _external=True, **values)
# 배포 환경에서 프로세스가 다시 떠도 로그인 세션이 유지되도록 고정 키를 우선 사용한다.
app.secret_key = os.environ.get("EGG_SECRET_KEY") or secrets.token_hex(32)

# 쿠키에는 로그인 정보와 진행 중인 연습 번호만 담는다. 답변·자기소개서는 서버에만 저장한다.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,                 # 자바스크립트에서 쿠키를 읽지 못하게 한다
    SESSION_COOKIE_SAMESITE="Lax",                # 다른 사이트에서 넘어온 요청에는 쿠키를 보내지 않는다
    SESSION_COOKIE_SECURE=flag("EGG_COOKIE_SECURE", PRODUCTION),   # 운영에서는 HTTPS로만 쿠키 전송
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,          # 녹음 파일 업로드 상한
)

with app.app_context():
    db.migrate_json()                             # 예전 JSON 파일이 있으면 한 번만 옮겨 온다
    db.cleanup_expired()


@app.before_request
def check_host():
    """설정된 주소가 아닌 Host 로 들어온 요청은 거절한다(Host 헤더 위조 방지)."""
    if not ALLOWED_HOSTS:
        return None
    host = (request.host or "").split(":")[0].lower()
    if host not in ALLOWED_HOSTS:
        abort(400, description="허용되지 않은 주소로 접속했습니다.")
    return None


@app.before_request
def require_https():
    """운영 환경에서 HTTP로 들어오면 HTTPS로 돌려보낸다."""
    if not FORCE_HTTPS or request.is_secure:
        return None
    if request.method not in ("GET", "HEAD"):
        abort(400, description="보안 연결(HTTPS)로 다시 접속해 주세요.")
    return redirect(request.url.replace("http://", "https://", 1), code=308)


def same_secret(sent, expected):
    """토큰을 안전하게 비교한다. 한글 등 ASCII가 아닌 값이 와도 예외가 나지 않는다."""
    if not expected:
        return False
    return hmac.compare_digest(str(sent).encode("utf-8", "ignore"),
                               str(expected).encode("utf-8", "ignore"))


def csrf_token():
    """세션마다 하나씩 발급하는 위조 방지 토큰."""
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


@app.before_request
def block_forged_requests():
    """다른 사이트가 사용자의 로그인 쿠키로 몰래 요청을 보내는 것(CSRF)을 막는다."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
    if not same_secret(sent, session.get("_csrf", "")):
        abort(400, description="요청이 만료되었거나 올바르지 않습니다. 페이지를 새로고침한 뒤 다시 시도해 주세요.")
    return None


def safe_next(target):
    """로그인 후 이동할 주소가 우리 사이트 내부 경로인지 확인한다.

    브라우저가 역슬래시를 슬래시로 바꿔 읽는 점을 이용한 '//evil.com' 우회를 막기 위해
    허용 문자를 아예 좁게 제한한다.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return None
    if not re.match(r"^/[A-Za-z0-9/_\-]*$", target):
        return None
    return target


@app.after_request
def set_privacy_headers(response):
    """답변·리포트가 캐시나 외부 사이트로 새지 않도록 기본 보호 헤더를 붙인다."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if FORCE_HTTPS:
        # 브라우저가 이후로는 항상 HTTPS로만 접속하도록 기억하게 한다
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.path not in ("/", "/login", "/signup"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response

# ========== Data & Config ==========
QUESTIONS = [
    {
        "tag": "지원동기",
        "text": "우리 회사와 이 직무에 지원한 이유를 본인의 경험과 연결해 설명해 주세요.",
        "tip": "회사의 구체적인 사업과 나의 경험을 한 문장으로 연결해 보세요.",
    },
    {
        "tag": "직무역량",
        "text": "지원 직무에서 가장 중요한 역량은 무엇이며, 그 역량을 기르기 위해 어떤 노력을 했나요?",
        "tip": "역량을 말한 뒤, 실제 행동과 결과를 함께 답하면 설득력이 높아집니다.",
    },
    {
        "tag": "문제해결",
        "text": "팀 프로젝트에서 의견 충돌이 있었던 경험과 이를 해결한 과정을 말해 주세요.",
        "tip": "상황보다 본인이 취한 행동과 그 결과를 자세히 설명해 보세요.",
    },
]


def public_research(company, role):
    """Collect public search evidence without requiring a paid API key."""
    if not company and not role:
        return []
    query = quote_plus(f"{company} {role} 채용 직무 역량 합격")
    url = f"https://html.duckduckgo.com/html/?q={query}"
    try:
        request_obj = Request(url, headers={"User-Agent": "EGG-interview-prototype/1.0"})
        page = urlopen(request_obj, timeout=8).read().decode("utf-8", errors="ignore")
        results = []
        for match in re.finditer(r'class="result__a" href="(.*?)"[^>]*>(.*?)</a>', page):
            link, title = match.groups()
            title = re.sub(r"<.*?>", "", html.unescape(title)).strip()
            link = html.unescape(link)
            if title and link.startswith("http"):
                results.append({"title": title, "url": link})
            if len(results) == 5:
                break
        return results
    except Exception:
        return []


QUESTION_COUNT = 5
DIFFICULTIES = ["기본", "실전", "압박"]

# 질문을 만들 때 먼저 다루는 항목 순서(직무와 가까운 것부터)
FIELD_PRIORITY = ["activities", "certificates", "language", "double_major", "department"]

# 적지 않은 항목을 압박 면접에서 되묻는 질문
PRESSURE_QUESTIONS = {
    "certificates": ("자격·전문성",
                     "{role} 관련 자격증이 없다고 적으셨습니다. 준비하지 않은 이유는 무엇이고, "
                     "그 공백을 무엇으로 대신할 수 있나요?",
                     "변명보다 대안을 말하세요. 자격증 대신 증명할 수 있는 결과물을 제시해 보세요."),
    "language": ("어학·글로벌",
                 "어학 성적을 제출하지 않으셨습니다. 외국어가 필요한 업무가 주어지면 어떻게 대응하시겠습니까?",
                 "지금 수준을 솔직히 말하고, 언제까지 어떻게 끌어올릴지 계획을 붙이세요."),
    "activities": ("대외활동",
                   "인턴이나 대외활동 경험이 없다고 적으셨는데, 그 기간에는 무엇을 하며 역량을 쌓으셨나요?",
                   "학교 안 활동이나 개인 프로젝트도 좋습니다. 무엇을 했고 무엇이 남았는지 말하세요."),
    "department": ("전공·기초",
                   "전공이 {role}과 직접 연결되지 않습니다. 이 직무를 감당할 수 있다고 보는 근거는 무엇인가요?",
                   "전공 대신 쌓은 지식과 그것을 증명한 경험을 함께 제시하세요."),
}

# 적어 넣은 항목을 바탕으로 만드는 질문
FILLED_QUESTIONS = {
    "certificates": ("자격·전문성",
                     "보유하신 {certificates} 자격이 {role} 업무에서 구체적으로 어떻게 쓰일 수 있다고 생각하나요?",
                     "자격증 이름만 말하지 말고, 그 지식을 실제로 써 본 장면을 함께 말해 보세요."),
    "language": ("어학·글로벌",
                 "{language} 성적을 갖추셨는데, 실제로 외국어를 활용해 성과를 낸 경험이 있다면 말씀해 주세요.",
                 "점수보다 '어떤 상황에서 어떻게 썼는지'가 설득력을 만듭니다."),
    "activities": ("대외활동",
                   "{activities} 경험에서 본인이 맡은 역할과, 그 과정에서 배운 것을 말씀해 주세요.",
                   "팀이 한 일이 아니라 본인이 한 일과 그 결과를 중심으로 답해 보세요."),
    "department": ("전공·기초",
                   "{department}에서 배운 것 중 {role} 업무에 가장 도움이 될 내용은 무엇인가요?",
                   "수업 이름 나열 대신, 배운 것을 적용해 본 경험 하나를 골라 설명해 보세요."),
    "double_major": ("전공·기초",
                     "{double_major}까지 공부하셨는데, 두 전공을 함께 살려 {role} 업무에 기여할 수 있는 부분은 무엇인가요?",
                     "두 전공이 만나는 지점을 하나의 사례로 보여 주면 설득력이 커집니다."),
}

# 여러 개를 적었을 때 개수를 짚어 주며 하나를 고르게 하는 질문
MULTI_QUESTIONS = {
    "certificates": ("자격·전문성",
                     "자격증을 {count}개 보유하셨습니다({items}). 이 중 {role} 업무에 가장 도움이 되는 것 하나를 고르고 "
                     "그 이유를 설명해 주세요.",
                     "고른 이유를 직무와 연결하고, 실제로 활용해 본 경험을 붙여 보세요."),
    "activities": ("대외활동",
                   "활동 경험이 {count}가지 있습니다({items}). 그중 본인에게 가장 큰 변화를 남긴 경험 하나를 골라 "
                   "무엇을 했고 무엇이 남았는지 말씀해 주세요.",
                   "여러 개를 나열하지 말고 하나를 깊게, 행동과 결과 중심으로 답해 보세요."),
}

# 학점이 낮을 때만 묻는 질문(성실성 확인)
GPA_QUESTION = ("성실성",
                "학점이 4.5 만점 기준 {gpa}로 다소 낮은 편입니다. 특별한 이유가 있었나요? "
                "그 시기에 학업 외에 집중한 일이 있다면 함께 말씀해 주세요.",
                "사실을 솔직히 밝히고, 그 기간에 무엇을 얻었는지와 이후 어떻게 달라졌는지까지 말해 보세요.")

GPA_QUESTION_PRESSURE = ("성실성",
                         "학점이 4.5 만점 기준 {gpa}입니다. 성실성을 의심할 수도 있는 수치인데, "
                         "면접관을 어떻게 설득하시겠습니까?",
                         "변명 대신 근거를 드세요. 그 시기의 우선순위와 이후의 변화를 숫자로 보여 주면 좋습니다.")

# 남는 자리를 채우는 공통 질문
GENERAL_QUESTIONS = [
    ("포부", "입사 후 3년 안에 {company}에서 이루고 싶은 목표는 무엇인가요?",
     "회사의 방향과 본인의 계획을 연결해 구체적인 시점과 함께 말해 보세요."),
    ("강점·보완", "본인의 가장 큰 강점과, 최근 보완하려 노력하고 있는 점을 하나씩 말씀해 주세요.",
     "약점은 인정하되 개선하려고 한 행동까지 함께 말하면 좋습니다."),
    ("협업", "함께 일하기 어려운 동료와 협업해야 한다면 어떻게 하시겠습니까?",
     "감정보다 방법을 말하세요. 실제로 겪은 상황이 있다면 근거로 붙이세요."),
]

PRESSURE_EXTRA = ("압박", "지금까지의 답변 중 가장 자신 없는 부분은 어디였고, 그 이유는 무엇인가요?",
                  "약한 곳을 스스로 짚고, 어떻게 메울지까지 말하면 오히려 신뢰를 얻습니다.")


ACTION_HINTS = ("개선", "분석", "제안", "설득", "달성", "기획", "개발", "운영", "수상",
                "인턴", "프로젝트", "공모전", "학회", "동아리", "매출", "전환")


def experience_hint(profile):
    """자기소개서에서 질문에 인용할 만한 한 대목을 고른다.

    파일로 올린 자기소개서는 길기 때문에 앞부분을 그대로 자르면 문장이 어색해진다.
    행동·성과가 담긴 문장을 골라 한 줄로 정리한다.
    """
    activities = profile.get("activities") or ""
    if not forms.is_blank_or_none(activities):
        return re.sub(r"\s+", " ", activities)[:40].strip()

    letter = re.sub(r"\s+", " ", profile.get("cover_letter") or "").strip()
    if not letter:
        return ""

    sentences = [part.strip() for part in re.split(r"[.!?]", letter) if len(part.strip()) > 12]
    if not sentences:
        return letter[:40].strip()

    chosen = next((part for part in sentences
                   if any(word in part for word in ACTION_HINTS)), sentences[0])
    if len(chosen) <= 42:
        return chosen
    cut = chosen[:42]
    space = cut.rfind(" ")
    return (cut[:space] if space > 20 else cut).strip()


def _fill(text, profile, role, company, extra=None):
    values = {
        "role": role,
        "company": company,
        "certificates": (profile.get("certificates") or "").strip(),
        "language": (profile.get("language") or "").strip(),
        "activities": (profile.get("activities") or "").strip(),
        "department": (profile.get("department") or "").strip(),
        "double_major": (profile.get("double_major") or "").strip(),
    }
    values.update(extra or {})
    return text.format(**values)


def build_questions(profile, difficulty="실전"):
    """프로필을 바탕으로 면접 질문 5개를 만든다.

    적어 넣은 항목은 그 내용으로 묻고, 비워 둔('없음') 항목은
    기본·실전에서는 건너뛰며 압박 면접에서만 준비하지 않은 이유를 되묻는다.

    학교(school)는 인적사항으로만 보관하고 질문에는 절대 사용하지 않는다(블라인드 면접).
    학점은 4.5 만점 환산 3.5 이하일 때만 성실성 확인 질문을 덧붙인다.
    """
    difficulty = difficulty if difficulty in DIFFICULTIES else "실전"
    role = (profile.get("role") or "").strip() or "지원 직무"
    company = (profile.get("company") or "").strip() or "지원 기업"
    experience = experience_hint(profile)

    questions = [
        {"tag": "지원동기",
         "text": f"{company}의 {role}에 지원한 이유를 본인의 경험과 연결해 설명해 주세요.",
         "tip": "회사의 공개 채용 정보에서 발견한 키워드와 나의 경험을 연결해 보세요."},
        {"tag": "직무역량",
         "text": (f"자기소개서에 적으신 '{experience}' 부분과 관련해, {role}에 필요한 역량을 하나 고르고 "
                  f"그 경험으로 어떻게 증명할 수 있는지 설명해 주세요."
                  if experience else f"{role}에 가장 필요한 역량은 무엇이며, 그것을 본인의 경험으로 어떻게 증명할 수 있나요?"),
         "tip": "역량을 말한 뒤, 본인의 행동과 측정 가능한 결과를 함께 답해 보세요."},
    ]

    # 어떤 프로필이든 반드시 들어가는 질문
    questions.append({"tag": "문제해결",
                      "text": "팀 프로젝트에서 의견 충돌이 있었던 경험과 이를 해결한 과정을 말해 주세요.",
                      "tip": "상황보다 본인이 취한 행동과 그 결과를 자세히 설명해 보세요."})

    def add(tag, text, tip, extra=None):
        if len(questions) < QUESTION_COUNT:
            questions.append({"tag": tag,
                              "text": _fill(text, profile, role, company, extra),
                              "tip": tip})

    # 학점이 낮을 때만 성실성을 확인한다(높으면 아예 묻지 않는다)
    gpa = forms.parse_gpa(profile.get("gpa"))
    if gpa and gpa["low"]:
        tag, text, tip = GPA_QUESTION_PRESSURE if difficulty == "압박" else GPA_QUESTION
        add(tag, text, tip, {"gpa": "%.2f" % gpa["normalized"]})

    # 압박 면접에서는 비워 둔 항목을 먼저 되묻는다(최대 2개)
    if difficulty == "압박":
        asked = 0
        for name in FIELD_PRIORITY:
            if asked >= 2 or len(questions) >= QUESTION_COUNT:
                break
            if name in forms.missing_fields(profile) and name in PRESSURE_QUESTIONS:
                add(*PRESSURE_QUESTIONS[name])
                asked += 1

    # 적어 넣은 항목으로 만드는 질문(경험 → 자격 → 어학 → 전공 순)
    filled = forms.filled_fields(profile)
    for name in FIELD_PRIORITY:
        if name not in filled:
            continue
        items = forms.split_items(profile.get(name))
        if len(items) > 1 and name in MULTI_QUESTIONS:
            # 쉼표로 여러 개를 적었으면 개수를 짚고 하나를 고르게 한다
            tag, text, tip = MULTI_QUESTIONS[name]
            add(tag, text, tip, {"count": len(items), "items": ", ".join(items[:4])})
        elif name in FILLED_QUESTIONS:
            add(*FILLED_QUESTIONS[name])

    if difficulty == "압박":
        add(*PRESSURE_EXTRA)

    for general in GENERAL_QUESTIONS:                        # 남는 자리를 공통 질문으로 채운다
        add(*general)

    if difficulty == "기본":
        for question in questions:
            question["tip"] = question["tip"] + " 편하게 말해도 괜찮아요."

    return questions[:QUESTION_COUNT]


ALLOWED_AUDIO = {".webm", ".ogg", ".m4a", ".mp4", ".wav", ".mp3", ".aac", ".flac"}


def transcribe_audio(audio_file):
    """Use the free local faster-whisper model when it is installed."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None, "브라우저 받아쓰기를 사용할 수 없고 서버 음성 인식(faster-whisper)도 설치되어 있지 않습니다. Chrome에서 다시 녹음하거나 텍스트로 답변해 주세요."

    # 알고 있는 오디오 확장자만 임시파일 이름에 쓴다(예상 못 한 파일명이 경로로 해석되지 않도록)
    extension = os.path.splitext(audio_file.filename or "")[1].lower()
    suffix = extension if extension in ALLOWED_AUDIO else ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as file:
        audio_file.save(file.name)
        path = file.name
    
    try:
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(path, language="ko")
        transcript = " ".join(segment.text.strip() for segment in segments).strip()
        return transcript, None
    except Exception as error:
        return None, f"음성 분석을 완료하지 못했습니다: {error}"
    finally:
        if os.path.exists(path):
            os.unlink(path)


def parse_voice_metrics(raw):
    """브라우저가 보낸 음향 측정값(JSON)을 숫자만 남겨 안전하게 읽어온다."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    metrics = {}
    for key, value in data.items():
        if isinstance(value, bool):
            metrics[key] = value
        elif isinstance(value, (int, float)):
            metrics[key] = value
    return metrics or None


def login_required(view):
    """로그인하지 않았거나 무효가 된 세션이면 로그인 화면으로 보낸다."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = session.get("user")
        if not user:
            return redirect(url_for("login_page", next=request.path))

        # 로그아웃·비밀번호 변경·탈퇴 뒤에 남은 쿠키는 세대가 달라 더 이상 통하지 않는다
        current = auth.session_version(user.get("email"))
        if current is None or current != user.get("session_version"):
            session.clear()
            return redirect(url_for("login_page", message="로그인이 만료되었습니다. 다시 로그인해 주세요."))
        return view(*args, **kwargs)
    return wrapper


@app.errorhandler(413)
def too_large(error):
    """업로드 용량을 넘겼을 때 기본 오류 대신 안내를 보여 준다."""
    message = "파일이 너무 큽니다. 자기소개서는 10MB 이하로 올려 주세요."
    if request.path.endswith("/extract"):
        return jsonify({"ok": False, "error": message}), 413
    if session.get("user"):
        return render_template("profile.html", error=message, form={}, hints={},
                               accept=documents.ACCEPT_ATTRIBUTE, difficulties=DIFFICULTIES,
                               difficulty="실전"), 413
    return render_template("login.html", error=message), 413


@app.context_processor
def inject_user():
    return {"current_user": session.get("user"), "csrf_token": csrf_token,
            "google_enabled": google_login.enabled()}


def current_email():
    return (session.get("user") or {}).get("email")


def active_run():
    """진행 중인 연습 기록을 서버 저장소에서 불러온다.

    다른 기기에서 로그인하거나 다시 로그인해 세션에 기록 번호가 없을 때는
    아직 끝내지 않은 가장 최근 연습을 이어서 진행할 수 있게 한다.
    """
    email = current_email()
    if not email:
        return None

    run_id = session.get("run_id")
    if run_id:
        run = storage.get_run(email, run_id)
        if run and not run.get("done"):
            return run

    run = storage.latest_run(email, done=False)
    if run:
        session["run_id"] = run["id"]
        session.modified = True
    return run


# 연습 횟수에 따른 성장 단계(마스코트 '애기'가 부화해 가는 과정)
LEVELS = [
    (0, 1, "갓 낳은 알", "첫 연습을 마치면 다음 단계로 올라가요!"),
    (1, 3, "따뜻해진 알", "%d번 더 연습하면 알에 금이 가요!"),
    (3, 6, "금이 간 알", "%d번 더 연습하면 부리가 보여요!"),
    (6, 10, "부리가 나온 알", "%d번 더 연습하면 병아리로 부화해요!"),
    (10, 10, "부화한 병아리", "부화 완료! 이제 실전에서 보여 줄 차례예요."),
]


def growth_level(finished):
    """완료한 면접 횟수를 성장 단계로 바꾼다."""
    for position, (start, target, name, hint) in enumerate(LEVELS):
        if finished < target or position == len(LEVELS) - 1:
            remaining = max(0, target - finished)
            # 진행바는 '이번 단계 안에서의 진척'이 아니라 목표 대비 전체 진척을 보여 준다
            ratio = int(round(min(1.0, finished / max(1, target)) * 100))
            return {
                "level": position + 1,
                "name": name,
                "hint": hint % remaining if "%d" in hint else hint,
                "current": finished,
                "target": target,
                "ratio": max(4, ratio),
            }
    return {"level": len(LEVELS), "name": LEVELS[-1][2], "hint": LEVELS[-1][3],
            "current": finished, "target": LEVELS[-1][1], "ratio": 100}


def week_bounds():
    """이번 주(월요일 0시부터)의 시작 시각."""
    today = datetime.now()
    start = today - timedelta(days=today.weekday())
    return start.replace(hour=0, minute=0, second=0, microsecond=0)


def practice_stats(email):
    """홈·성장 기록 화면에서 함께 쓰는 통계."""
    runs = [storage.summarize_run(run) for run in storage.list_runs(email)]
    finished = [run for run in runs if run["done"] and run["total"] is not None]

    monday = week_bounds()
    def in_this_week(run):
        try:
            return datetime.strptime(run["created_label"], "%Y.%m.%d %H:%M") >= monday
        except (TypeError, ValueError):
            return False

    weekly = [run for run in runs if in_this_week(run)]
    scores = [run["total"] for run in reversed(finished)][-6:]      # 오래된 것부터 최근 6회
    highest = max(scores) if scores else 100
    trend = [{"score": score, "height": max(12, int(round(score / max(highest, 1) * 100)))}
             for score in scores]

    delta = None
    if len(finished) >= 2:
        delta = finished[0]["total"] - finished[1]["total"]         # 직전 회차 대비 변화

    return {
        "runs": runs,
        "finished_runs": finished,
        "finished": len(finished),
        "average": round(sum(run["total"] for run in finished) / len(finished)) if finished else None,
        "best": max(finished, key=lambda run: run["total"]) if finished else None,
        "pass_rate": finished[0]["pass_rate"] if finished else None,
        "delta": delta,
        "trend": trend,
        "this_week": len([run for run in weekly if run["done"]]),
        "week": {
            "finished": len([run for run in weekly if run["done"]]),
            "answers": sum(run["answer_count"] for run in weekly),
            "voice": sum(run["voice_count"] or 0 for run in weekly),
        },
        "pending": next((run for run in runs if not run["done"] and run["question_count"]), None),
        "last": finished[0] if finished else (runs[0] if runs else None),
    }


# ========== Routes ==========
@app.route("/")
def home():
    email = current_email()
    if not email:
        return render_template("home.html", stats=None, level=growth_level(0))
    stats = practice_stats(email)
    return render_template("home.html", stats=stats, level=growth_level(stats["finished"]))


@app.route("/signup", methods=["GET", "POST"])
def signup_page():
    if session.get("user"):
        return redirect(url_for("profile_page"))

    if request.method == "POST":
        form = request.form
        throttled = auth.signup_throttled(request.remote_addr)
        if throttled:
            return render_template("signup.html", error=throttled,
                                   form={"name": form.get("name"), "email": form.get("email")})
        auth.record_signup_attempt(request.remote_addr)

        error = auth.validate(form.get("email"), form.get("password"),
                              form.get("name"), form.get("confirm"))
        if not error:
            user, error = auth.create_user(form.get("email"), form.get("password"), form.get("name"))
            if user:
                session.clear()
                session["user"] = user
                session["just_signed_up"] = "email"     # 완료 화면을 한 번 보여 주기 위한 표시
                return redirect(url_for("welcome_page"))
        return render_template("signup.html", error=error,
                               form={"name": form.get("name"), "email": form.get("email")})

    return render_template("signup.html", form={})


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if session.get("user") and request.method == "GET":
        return redirect(url_for("profile_page"))

    if request.method == "POST":
        email = request.form.get("email")
        blocked = auth.lockout_message(email, request.remote_addr)
        if blocked:
            return render_template("login.html", error=blocked, email=email)

        user, error = auth.verify_user(email, request.form.get("password"))
        if user:
            auth.clear_failures(email, request.remote_addr)
            session.clear()
            session["user"] = user
            target = safe_next(request.args.get("next") or request.form.get("next"))
            return redirect(target or url_for("profile_page"))

        auth.record_failure(email, request.remote_addr)
        return render_template("login.html", error=error, email=email)

    return render_template("login.html", message=request.args.get("message"))


def google_redirect_uri():
    """구글 콘솔에 등록해야 하는 콜백 주소."""
    return external_url("google_callback")


@app.route("/auth/google")
def google_start():
    """구글 동의 화면으로 보낸다."""
    if not google_login.enabled():
        return redirect(url_for("login_page", message="구글 로그인이 아직 설정되지 않았습니다. 이메일로 로그인해 주세요."))

    state = google_login.make_state()
    session["google_state"] = state                    # 돌아온 요청이 우리가 보낸 것인지 확인하는 값
    session["google_next"] = safe_next(request.args.get("next")) or ""
    return redirect(google_login.authorize_url(google_redirect_uri(), state))


@app.route("/auth/google/callback")
def google_callback():
    """구글이 돌려보낸 요청을 처리해 로그인시킨다."""
    expected = session.pop("google_state", None)
    target = session.pop("google_next", "") or None

    if request.args.get("error"):
        return redirect(url_for("login_page", message="구글 로그인을 취소했습니다."))

    state = request.args.get("state")
    if not state or not same_secret(state, expected):
        return render_template("login.html", error="구글 로그인 요청이 만료되었습니다. 다시 시도해 주세요."), 400

    code = request.args.get("code")
    if not code:
        return render_template("login.html", error="구글에서 인증 정보를 받지 못했습니다."), 400

    profile, error = google_login.login_with_code(code, google_redirect_uri())
    if error:
        return render_template("login.html", error=error), 400

    user, error = auth.login_or_create_google_user(profile)
    if error:
        return render_template("login.html", error=error), 400

    auth.clear_failures(user["email"], request.remote_addr)
    is_new = bool(user.pop("is_new", False))
    session.clear()
    session["user"] = user
    if is_new and not target:
        session["just_signed_up"] = "google"
        return redirect(url_for("welcome_page"))
    return redirect(target or url_for("profile_page"))


@app.route("/password/set", methods=["POST"])
@login_required
def set_password():
    """구글로 가입한 계정에 비밀번호를 추가한다(이메일 로그인도 쓰고 싶을 때)."""
    done, error = auth.set_password(current_email(), request.form.get("new_password"),
                                    request.form.get("confirm"))
    if done:
        session.clear()
        return redirect(url_for("login_page", message="비밀번호를 설정했습니다. 이제 이메일로도 로그인할 수 있어요."))
    return redirect(url_for("mypage", password_error=error))


@app.route("/logout")
def logout():
    user = session.get("user")
    if user:
        auth.bump_session_version(user.get("email"))   # 남아 있는 쿠키를 더는 쓸 수 없게 한다
    session.clear()
    return redirect(url_for("login_page", message="로그아웃했습니다. 다음 연습 때 또 만나요."))


@app.route("/forgot", methods=["GET", "POST"])
def forgot_page():
    """비밀번호 재설정 요청.

    메일 발송 설정(EGG_RESET_LINK_TO_LOG 등)이 없으면 링크를 서버 로그에만 남긴다.
    화면에는 가입 여부와 관계없이 같은 안내를 보여 계정 존재 여부가 드러나지 않게 한다.
    """
    if request.method == "POST":
        email = request.form.get("email")
        # 같은 사람에게 재설정 메일을 반복해서 보내는 것을 막는다
        blocked = (auth.check_limit("reset", email, request.remote_addr)
                   or auth.lockout_message(email, request.remote_addr))
        if not blocked:
            auth.record_attempt("reset", email, request.remote_addr)
            token = auth.create_reset_token(email)
            if token:
                link = external_url("reset_password_page", token=token)
                if mailer.enabled():
                    sent, error = mailer.send_reset_link(auth.normalize_email(email), link)
                    if not sent:
                        # 메일이 실패해도 화면 안내는 같게 두고, 운영자가 볼 수 있게 남긴다
                        app.logger.error("[비밀번호 재설정] 메일 발송 실패: %s", error)
                        app.logger.warning("[비밀번호 재설정] %s → %s", auth.normalize_email(email), link)
                else:
                    app.logger.warning("[비밀번호 재설정] %s → %s", auth.normalize_email(email), link)
        return render_template("forgot.html", done=True, throttled=bool(blocked))

    return render_template("forgot.html")


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_password_page(token):
    if not auth.email_for_token(token):
        return render_template("reset.html", invalid=True)

    if request.method == "POST":
        done, error = auth.consume_reset_token(token, request.form.get("password"),
                                               request.form.get("confirm"))
        if done:
            session.clear()
            return redirect(url_for("login_page", message="비밀번호를 변경했습니다. 새 비밀번호로 로그인해 주세요."))
        return render_template("reset.html", error=error, token=token)

    return render_template("reset.html", token=token)


@app.route("/password", methods=["POST"])
@login_required
def change_password():
    email = current_email()
    done, error = auth.change_password(email, request.form.get("current_password"),
                                       request.form.get("new_password"), request.form.get("confirm"))
    if done:
        session.clear()                                # 비밀번호가 바뀌면 모든 기기에서 다시 로그인
        return redirect(url_for("login_page", message="비밀번호를 변경했습니다. 새 비밀번호로 로그인해 주세요."))
    return redirect(url_for("mypage", password_error=error))


@app.route("/welcome")
@login_required
def welcome_page():
    """회원가입 직후 한 번만 보여 주는 완료 화면."""
    joined = session.pop("just_signed_up", None)
    if not joined:
        return redirect(url_for("profile_page"))
    return render_template("welcome.html", joined=joined)


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile_page():
    if request.method == "POST":
        data = request.form
        cover_letter = (data.get("cover_letter") or "").strip()

        # 'X', '-', '없습니다' 같은 값을 '없음' 하나로 맞추고 특수문자를 걸러 낸다
        cleaned, hints, blocking = forms.clean_profile(data)
        if blocking:
            return render_template("profile.html", form=dict(data, **cleaned), hints=hints,
                                   accept=documents.ACCEPT_ATTRIBUTE, difficulties=DIFFICULTIES,
                                   difficulty=data.get("difficulty", "실전"),
                                   error="입력할 수 없는 문자가 있어요. 표시된 칸을 고쳐 주세요.")

        # 파일로 올린 자기소개서가 있으면 글자를 뽑아 합친다
        # (자바스크립트가 막힌 환경에서도 업로드가 동작하도록 서버에서 한 번 더 처리한다)
        upload = request.files.get("cover_letter_file")
        notice = None
        if upload and upload.filename:
            blocked = auth.check_limit("upload", current_email(), request.remote_addr)
            if blocked:
                return render_template("profile.html", error=blocked, form=dict(data, **cleaned),
                                       hints=hints, accept=documents.ACCEPT_ATTRIBUTE,
                                       difficulties=DIFFICULTIES,
                                       difficulty=data.get("difficulty", "실전")), 429
            auth.record_attempt("upload", current_email(), request.remote_addr)

            result, error = documents.extract(upload)
            if error:
                return render_template("profile.html", error=error, form=dict(data, **cleaned),
                                       hints=hints, accept=documents.ACCEPT_ATTRIBUTE,
                                       difficulties=DIFFICULTIES,
                                       difficulty=data.get("difficulty", "실전"))
            if result["text"] not in cover_letter:
                cover_letter = (cover_letter + "\n\n" + result["text"]).strip() if cover_letter else result["text"]
            notice = "%s 에서 %d자를 불러왔어요." % (result["filename"], result["chars"])

        if not cover_letter:
            return render_template("profile.html", form=dict(data, **cleaned), hints=hints,
                                   accept=documents.ACCEPT_ATTRIBUTE, difficulties=DIFFICULTIES,
                                   difficulty=data.get("difficulty", "실전"),
                                   error="자기소개서를 붙여넣거나 파일로 올려 주세요. 질문을 만드는 데 필요해요.")

        difficulty = data.get("difficulty") if data.get("difficulty") in DIFFICULTIES else "실전"
        profile = {
            "school": cleaned["school"],            # 인적사항으로만 보관(질문에는 쓰지 않음)
            "department": cleaned["department"],
            "double_major": cleaned["double_major"],
            "gpa": cleaned["gpa"],
            "company": cleaned["company"],
            "role": cleaned["role"],
            "certificates": cleaned["certificates"],
            "language": cleaned["language"],
            "activities": cleaned["activities"],
            "cover_letter": cover_letter,
            "difficulty": difficulty,
        }
        run = storage.new_run(profile, build_questions(profile, difficulty))
        run["difficulty"] = difficulty
        run["notice"] = notice
        storage.save_run(current_email(), run)
        session["run_id"] = run["id"]          # 쿠키에는 기록 번호만 남긴다
        session.modified = True
        return redirect(url_for("interview_page"))

    return render_template("profile.html", form={}, hints={}, accept=documents.ACCEPT_ATTRIBUTE,
                           difficulties=DIFFICULTIES, difficulty="실전",
                           none_text=forms.NONE_TEXT)


@app.route("/profile/extract", methods=["POST"])
@login_required
def extract_cover_letter():
    """업로드한 파일에서 글자만 뽑아 돌려준다(화면에서 바로 확인할 수 있게)."""
    blocked = auth.check_limit("upload", current_email(), request.remote_addr)
    if blocked:
        return jsonify({"ok": False, "error": blocked}), 429
    auth.record_attempt("upload", current_email(), request.remote_addr)

    result, error = documents.extract(request.files.get("cover_letter_file"))
    if error:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "text": result["text"], "chars": result["chars"],
                    "truncated": result["truncated"], "filename": result["filename"]})


@app.route("/interview", methods=["GET", "POST"])
@login_required
def interview_page():
    run = active_run()
    if not run:
        return redirect(url_for("profile_page"))

    profile = run.get("profile", {})
    questions = run.get("questions") or QUESTIONS
    index = run.get("index", 0)

    if index >= len(questions):
        return redirect(url_for("report_page"))

    def again(message):
        return render_template("interview.html", error=message, profile=profile,
                               question=questions[index], index=index, total=len(questions),
                               difficulty=run.get("difficulty") or profile.get("difficulty") or "실전")

    if request.method == "POST":
        is_voice = request.form.get("input_mode") == "voice"
        answer_text = request.form.get("answer", "").strip()
        mode = "텍스트"

        if is_voice:
            mode = "음성"
            answer_text = request.form.get("speech_transcript", "").strip()
            audio_file = request.files.get("audio")

            # 브라우저 받아쓰기 결과가 없으면 업로드된 녹음 파일을 서버에서 분석한다.
            if not answer_text and audio_file and audio_file.filename:
                transcript, transcribe_error = transcribe_audio(audio_file)
                if transcript:
                    answer_text = transcript
                else:
                    return again(transcribe_error or "녹음 내용을 글로 옮기지 못했습니다. 다시 녹음해 주세요.")

            if not answer_text:
                return again("녹음에서 답변 내용을 찾지 못했습니다. 마이크 가까이에서 다시 말해 주세요.")

        if not answer_text:
            return again("답변을 한두 문장이라도 적어주세요.")

        metrics = parse_voice_metrics(request.form.get("voice_metrics")) if is_voice else None
        evaluation = scoring.evaluate_answer(questions[index]["text"], answer_text, metrics,
                                             profile, questions[index].get("tag"))

        run["answers"].append({
            "question": questions[index]["text"],
            "answer": answer_text,
            "mode": mode,
            "evaluation": evaluation,
        })

        if index == len(questions) - 1:
            run["index"] = len(questions)
            run["done"] = True
            run["summary"] = scoring.summarize(run["answers"], profile)
            storage.save_run(current_email(), run)
            return redirect(url_for("report_page", run_id=run["id"]))

        run["index"] = index + 1
        storage.save_run(current_email(), run)
        return redirect(url_for("interview_page"))

    return render_template("interview.html", profile=profile, question=questions[index],
                           index=index, total=len(questions),
                           difficulty=run.get("difficulty") or profile.get("difficulty") or "실전")


@app.route("/report")
@app.route("/report/<run_id>")
@login_required
def report_page(run_id=None):
    email = current_email()
    if run_id:
        run = storage.get_run(email, run_id)
    else:
        # 방금 끝낸 연습을 먼저 보여 준다. 없으면 진행 중인 연습의 중간 결과를 보여 준다.
        run = storage.latest_run(email, done=True)
        if not run or not run.get("answers"):
            in_progress = active_run()
            if in_progress and in_progress.get("answers"):
                run = in_progress
    if not run or not run.get("answers"):
        return redirect(url_for("profile_page"))

    profile = run.get("profile", {})
    answers = run.get("answers", [])
    summary = run.get("summary") or scoring.summarize(answers, profile)

    text = " ".join(item["answer"] for item in answers)
    detail = min(92, 48 + len(re.findall(r"[가-힣A-Za-z0-9]", text)) // 4)
    if summary:
        readiness = summary["total"]
        pass_rate = summary["pass_rate"]
    else:
        readiness = min(94, 61 + len(answers) * 5 + (8 if profile.get("cover_letter") else 0))
        pass_rate = min(88, 54 + readiness // 5)

    return render_template("report.html", profile=profile, answers=answers,
                           readiness=readiness, detail=detail, pass_rate=pass_rate,
                           summary=summary, date=run.get("created_label", ""),
                           run=run, saved=bool(run.get("done")))


@app.route("/mypage")
@login_required
def mypage():
    stats = practice_stats(current_email())
    return render_template("mypage.html", runs=stats["runs"], finished=stats["finished"],
                           best=stats["best"], average=stats["average"], trend=stats["trend"],
                           week=stats["week"], level=growth_level(stats["finished"]),
                           password_error=request.args.get("password_error"))


@app.route("/mypage/delete/<run_id>", methods=["POST"])
@login_required
def delete_run(run_id):
    storage.delete_run(current_email(), run_id)
    if session.get("run_id") == run_id:
        session.pop("run_id", None)
    return redirect(url_for("mypage"))


@app.route("/mypage/delete-all", methods=["POST"])
@login_required
def delete_all_runs():
    storage.delete_all(current_email())
    session.pop("run_id", None)
    return redirect(url_for("mypage"))


@app.route("/mypage/withdraw", methods=["POST"])
@login_required
def withdraw():
    email = current_email()
    storage.delete_all(email)          # 연습 기록 먼저 삭제
    auth.delete_user(email)            # 계정 정보 삭제
    session.clear()
    return redirect(url_for("login_page", message="계정과 모든 연습 기록을 삭제했습니다."))


@app.route("/reset")
def reset():
    session.pop("run_id", None)       # 로그인 상태는 유지하고 진행 중인 연습만 놓아준다
    return redirect(url_for("home"))


if __name__ == "__main__":
    # 디버거는 원격 코드 실행 위험이 있으므로 명시적으로 켤 때만 사용한다
    app.run(debug=os.environ.get("EGG_DEBUG", "").lower() in ("1", "true", "yes"))
