import random
import re
import html
import os
import tempfile
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, session, redirect, url_for, jsonify
import secrets

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

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


def build_questions(profile):
    """Turn the user's profile into interview prompts for the current session."""
    role = profile.get("role") or "지원 직무"
    company = profile.get("company") or "지원 기업"
    experience = profile.get("activities") or profile.get("cover_letter") or "가장 자신 있는 경험"
    return [
        {"tag": "지원동기", "text": f"{company}의 {role}에 지원한 이유를 본인의 경험과 연결해 설명해 주세요.", "tip": "회사의 공개 채용 정보에서 발견한 키워드와 나의 경험을 연결해 보세요."},
        {"tag": "직무역량", "text": f"{role}에 필요한 역량을 하나 고르고, {experience[:35]} 경험으로 증명해 주세요.", "tip": "역량을 말한 뒤, 본인의 행동과 측정 가능한 결과를 함께 답해 보세요."},
        {"tag": "문제해결", "text": "팀 프로젝트에서 의견 충돌이 있었던 경험과 이를 해결한 과정을 말해 주세요.", "tip": "상황보다 본인이 취한 행동과 그 결과를 자세히 설명해 보세요."},
    ]


def transcribe_audio(audio_file):
    """Use the free local faster-whisper model when it is installed."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None, "faster-whisper가 설치되지 않아 녹음 파일만 저장했습니다. requirements.txt 설치 후 다시 시도해 주세요."
    
    suffix = ".wav"
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


# ========== Routes ==========
@app.route("/")
def home():
    return render_template("home.html")


@app.route("/profile", methods=["GET", "POST"])
def profile_page():
    if request.method == "POST":
        data = request.form
        session["profile"] = {
            "department": data.get("department"),
            "company": data.get("company"),
            "role": data.get("role"),
            "certificates": data.get("certificates"),
            "language": data.get("language"),
            "activities": data.get("activities"),
            "cover_letter": data.get("cover_letter"),
        }
        session["research"] = public_research(data.get("company"), data.get("role"))
        session["questions"] = build_questions(session["profile"])
        session["question_index"] = 0
        session["answers"] = []
        session.modified = True
        return redirect(url_for("interview_page"))
    
    return render_template("profile.html")


@app.route("/interview", methods=["GET", "POST"])
def interview_page():
    if "profile" not in session:
        return redirect(url_for("profile_page"))
    
    profile = session.get("profile", {})
    questions = session.get("questions", QUESTIONS)
    index = session.get("question_index", 0)
    
    if index >= len(questions):
        return redirect(url_for("report_page"))
    
    if request.method == "POST":
        answer_text = request.form.get("answer", "").strip()
        if not answer_text:
            return render_template("interview.html", error="답변을 한두 문장이라도 적어주세요.", 
                                 profile=profile, question=questions[index], index=index, 
                                 total=len(questions))
        
        answers = session.get("answers", [])
        answers.append({
            "question": questions[index]["text"],
            "answer": answer_text,
            "mode": "텍스트"
        })
        session["answers"] = answers
        
        if index == len(questions) - 1:
            return redirect(url_for("report_page"))
        else:
            session["question_index"] = index + 1
            session.modified = True
            return redirect(url_for("interview_page"))
    
    return render_template("interview.html", profile=profile, question=questions[index], 
                         index=index, total=len(questions))


@app.route("/report")
def report_page():
    if "profile" not in session or "answers" not in session:
        return redirect(url_for("profile_page"))
    
    profile = session.get("profile", {})
    answers = session.get("answers", [])
    text = " ".join(item["answer"] for item in answers)
    
    detail = min(92, 48 + len(re.findall(r"[가-힣A-Za-z0-9]", text)) // 4)
    readiness = min(94, 61 + len(answers) * 5 + (8 if profile.get("cover_letter") else 0))
    pass_rate = min(88, 54 + readiness // 5)
    
    today = datetime.now().strftime('%Y.%m.%d')
    return render_template("report.html", profile=profile, answers=answers,
                         readiness=readiness, detail=detail, pass_rate=pass_rate,
                         date=today)


@app.route("/reset")
def reset():
    session.clear()
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(debug=True)
