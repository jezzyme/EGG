import random
import re
import html
import os
import tempfile
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from datetime import datetime

import streamlit as st

st.set_page_config(
    page_title="EGG | 맞춤형 모의면접",
    page_icon="🥚",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ---------- App state ----------
if "page" not in st.session_state:
    st.session_state.page = "home"
if "profile" not in st.session_state:
    st.session_state.profile = {}
if "question_index" not in st.session_state:
    st.session_state.question_index = 0
if "answers" not in st.session_state:
    st.session_state.answers = []
if "show_result" not in st.session_state:
    st.session_state.show_result = False
if "research" not in st.session_state:
    st.session_state.research = []

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
        request = Request(url, headers={"User-Agent": "EGG-interview-prototype/1.0"})
        page = urlopen(request, timeout=8).read().decode("utf-8", errors="ignore")
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


def transcribe_audio(audio):
    """Use the free local faster-whisper model when it is installed."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None, "faster-whisper가 설치되지 않아 녹음 파일만 저장했습니다. requirements.txt 설치 후 다시 시도해 주세요."
    suffix = ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as file:
        file.write(audio.getvalue())
        path = file.name
    try:
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(path, language="ko")
        transcript = " ".join(segment.text.strip() for segment in segments).strip()
        return transcript, None
    except Exception as error:
        return None, f"음성 분석을 완료하지 못했습니다: {error}"
    finally:
        os.unlink(path)

# ---------- Styling ----------
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Gowun+Dodum&family=Jua&display=swap');
:root { --ink:#202a25; --muted:#758077; --cream:#fbfaf5; --lime:#e8f0df; --yellow:#f5bd35; --orange:#e99720; --line:#e3e8de; --green:#26392f; }
.stApp { background: radial-gradient(circle at 85% 7%, #fff1b9 0, #fff8d9 14%, transparent 33%), linear-gradient(135deg, #fbfaf5 0%, #f6f6ed 52%, #edf4e6 100%); color:var(--ink); }
.block-container { max-width: 1050px; padding: 30px 6vw 55px; }
html, body, [class*="css"] { font-family:'Gowun Dodum', sans-serif; }
h1, h2, h3 { font-family:'Jua', sans-serif !important; color:var(--ink) !important; letter-spacing:0 !important; }
.eg-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:42px; }
.brand { display:flex; align-items:center; gap:11px; font-family:'Jua'; font-size:24px; }
.egg-mark { width:22px; height:29px; display:inline-block; border-radius:53% 47% 50% 50% / 45% 43% 57% 55%; background:#f7c63e; position:relative; }
.egg-mark:after { content:''; position:absolute; width:7px; height:8px; top:4px; left:6px; border-radius:50%; background:#fff2bb; }
.motto { background:#e7eddf; color:#6a756a; border-radius:22px; padding:9px 16px; font-size:12px; }
.eyebrow { color:#d58b16; font-weight:700; font-size:14px; margin-bottom:10px; }
.hero-title { font-family:'Jua'; font-size:clamp(38px,5vw,65px); line-height:1.12; margin:0 0 18px; }
.hero-title span { color:#df941d; }
.lead { font-size:16px; line-height:1.8; color:#667169; max-width:445px; }
.step-row { display:flex; gap:10px; margin:27px 0 0; flex-wrap:wrap; }
.step { background:#fff; border-radius:12px; padding:12px 15px; font-size:13px; box-shadow:0 4px 16px #75807713; }
.step strong { color:#d98f13; margin-left:8px; }
.phone-trio { display:flex; justify-content:center; align-items:end; gap:16px; margin:34px 0 6px; }
.phone { width:148px; height:320px; border:7px solid #26342c; border-radius:29px; background:#fffdf8; padding:31px 12px 12px; position:relative; box-shadow:0 14px 30px #59624b22; }
.phone:nth-child(2) { height:344px; transform:translateY(-20px); }
.phone-notch { position:absolute; top:-1px; left:47px; width:50px; height:14px; background:#26342c; border-radius:0 0 9px 9px; }
.phone small { font-size:8px; color:#7b847b; }
.phone h4 { font-family:'Jua'; font-size:15px; margin:18px 0 10px; }
.phone-card { background:#fff2cd; border-radius:13px; padding:13px 10px; margin:10px 0; font-size:10px; line-height:1.5; }
.phone-bar { height:5px; width:85%; background:#eca927; border-radius:5px; margin:16px 0; }
.phone-button { background:#263c31; color:#fff; border-radius:9px; padding:10px; text-align:center; font-size:9px; position:absolute; bottom:12px; left:12px; right:12px; }
.footer-note { color:#8a9289; font-size:11px; margin-top:55px; }
.pill { display:inline-block; background:#e8f0df; color:#4c6351; border-radius:16px; padding:6px 10px; font-size:12px; margin-right:5px; }
.metric { background:#fffdf8; border:1px solid var(--line); border-radius:16px; padding:18px; text-align:center; }
.metric b { display:block; font-family:'Jua'; font-size:30px; color:#293d31; }
.insight { background:#eaf1e4; border-radius:14px; padding:18px; line-height:1.7; font-size:14px; }
[data-testid="stForm"] { background:rgba(255,255,255,.68); border:1px solid var(--line); border-radius:18px; padding:22px; }
.stButton > button, .stFormSubmitButton > button { border-radius:11px; border:0; background:#263c31; color:white; min-height:44px; font-weight:700; }
.stButton > button:hover, .stFormSubmitButton > button:hover { background:#d98f13; color:white; }
@media (max-width: 640px) { .block-container { padding:20px 20px 40px; } .eg-header { margin-bottom:34px; } .motto { display:none; } .phone-trio { gap:7px; } .phone { width:31vw; max-width:145px; height:275px; padding:27px 8px 8px; } .phone:nth-child(2) { height:298px; } .phone-notch { left:calc(50% - 23px); width:46px; } .phone h4 { font-size:12px; } .phone-card { font-size:9px; padding:10px 7px; } .hero-title { font-size:42px; } }
</style>
""",
    unsafe_allow_html=True,
)


def header():
    st.markdown('<div class="eg-header"><div class="brand"><i class="egg-mark"></i> EGG <span style="font-family: Gowun Dodum; font-size:11px; color:#778078; line-height:1.1;">Interview<br>Lab</span></div><div class="motto">나의 가능성을 한 단계 더 익히는 시간</div></div>', unsafe_allow_html=True)


def go(page):
    st.session_state.page = page
    st.rerun()


def home():
    header()
    left, right = st.columns([1, 1.3], vertical_alignment="center")
    with left:
        st.markdown('<div class="eyebrow">AI 맞춤형 모의면접 앱</div><div class="hero-title">합격의 감각을<br><span>차근차근 익히는</span><br>면접 연습</div><div class="lead">나의 스펙과 자기소개서를 바탕으로 질문을 만들고,<br>답변의 밀도와 방향을 분석해 다음 연습을 제안해요.</div>', unsafe_allow_html=True)
        st.markdown('<div class="step-row"><div class="step">정보를 담고 <strong>→</strong></div><div class="step">답변을 익히고 <strong>→</strong></div><div class="step">합격에 가까워져요</div></div>', unsafe_allow_html=True)
        st.write("")
        if st.button("내 면접 재료 담으러 가기  →", use_container_width=True):
            go("profile")
    with right:
        st.markdown('<div class="phone-trio"><div class="phone"><div class="phone-notch"></div><small>9:41　　•••</small><h4>나만의 취업 바구니</h4><div class="phone-bar"></div><div class="phone-card"><b>경영학과 · 마케팅 직무</b><br>OPEN AI　　인턴 1회</div><small>추가하면 좋은 정보</small><div class="phone-button">내 면접 재료 완성하기</div></div><div class="phone"><div class="phone-notch"></div><small>9:41　　•••</small><h4>EGG 코치</h4><div class="phone-card"><b>데이터 분석 경험 중,<br>의사결정에 가장 크게 기여한 사례를 들려주세요.</b></div><div style="font-size:28px; text-align:center; color:#eca927; margin-top:20px;">⌁⌁⌁</div><div class="phone-button">답변 완료</div></div><div class="phone"><div class="phone-notch"></div><small>9:41　　•••</small><h4>오늘의 면접 리포트</h4><div style="font-family:Jua; font-size:24px; text-align:center; margin:35px 0 18px;">78%</div><small>현재 취업 준비도 · 잘 익어가고 있어요</small><div class="phone-card" style="background:#e8f0df;"><b>EGG 코치의 한마디</b><br>경험의 깊이는 충분해요. 이제 답변에 숫자와 결과를 더해보세요.</div><div class="phone-button">맞춤 피드백 전체 보기</div></div></div>', unsafe_allow_html=True)
    st.markdown('<div class="footer-note"><b>EGG</b>는 정보를 수집하는 것에서 끝나지 않고, <b>답변 → 피드백 → 재연습</b>의 루틴을 만듭니다.</div>', unsafe_allow_html=True)


def profile():
    header()
    st.markdown("## 나의 면접 재료 담기")
    st.caption("입력한 정보는 나에게 맞는 질문과 피드백을 만드는 데 사용돼요.")
    with st.form("profile_form"):
        col1, col2 = st.columns(2)
        with col1:
            department = st.text_input("학과 / 전공", placeholder="예: 경영학과")
            target_company = st.text_input("관심 기업", placeholder="예: 카카오")
            target_role = st.text_input("관심 직무", placeholder="예: 서비스 기획")
        with col2:
            certificates = st.text_input("자격증", placeholder="예: ADsP, SQLD")
            language = st.text_input("어학 성적", placeholder="예: TOEIC 890")
            activities = st.text_input("인턴 · 대외활동 · 수상", placeholder="예: 앱 공모전 대상")
        cover_letter = st.text_area("자기소개서 또는 강조하고 싶은 경험", height=130, placeholder="가장 자신 있는 경험을 자유롭게 적어주세요.")
        uploaded = st.file_uploader("관련 문서 추가 (선택)", type=["pdf", "docx", "txt"], accept_multiple_files=True)
        submitted = st.form_submit_button("면접 재료 저장하고 질문 받기  →", use_container_width=True)
    if submitted:
        st.session_state.profile = {"department": department, "company": target_company, "role": target_role, "certificates": certificates, "language": language, "activities": activities, "cover_letter": cover_letter, "files": uploaded or []}
        st.session_state.research = public_research(target_company, target_role)
        st.session_state.questions = build_questions(st.session_state.profile)
        st.session_state.question_index = 0
        st.session_state.answers = []
        go("interview")
    if st.button("← 처음으로 돌아가기"):
        go("home")


def interview():
    header()
    profile = st.session_state.profile
    role = profile.get("role") or "관심 직무"
    company = profile.get("company") or "지원 기업"
    index = st.session_state.question_index
    questions = st.session_state.get("questions", QUESTIONS)
    question = questions[index]
    st.markdown(f'<span class="pill">실전 연습 {index + 1} / {len(QUESTIONS)}</span><span class="pill">{question["tag"]}</span>', unsafe_allow_html=True)
    st.markdown(f"## {company} {role} 면접")
    st.progress(index / len(QUESTIONS))
    st.markdown(f'<div class="insight"><b>EGG 코치의 질문</b><br><span style="font-size:19px; line-height:1.55;">{question["text"]}</span></div>', unsafe_allow_html=True)
    st.caption(f"답변 힌트: {question['tip']}")
    tab_text, tab_voice = st.tabs(["텍스트로 답하기", "녹음으로 답하기"])
    with tab_text:
        answer = st.text_area("답변", height=180, placeholder="결론부터 말하고, 경험과 결과를 차례로 설명해 보세요.", label_visibility="collapsed", key=f"answer_{index}")
        if st.button("답변 제출", use_container_width=True, key=f"submit_text_{index}"):
            if not answer.strip():
                st.warning("답변을 한두 문장이라도 적어주세요.")
            else:
                submit_answer(answer, "텍스트")
    with tab_voice:
        audio_input = getattr(st, "audio_input", None)
        if audio_input:
            audio = audio_input("녹음 시작", key=f"audio_{index}")
            if audio:
                st.audio(audio)
                if st.button("녹음 제출", use_container_width=True, key=f"submit_audio_{index}"):
                    transcript, error = transcribe_audio(audio)
                    if error:
                        st.warning(error)
                    elif transcript:
                        st.success("음성을 텍스트로 변환해 분석할 준비가 됐어요.")
                        submit_answer(transcript, "음성 · Whisper 전사")
                    else:
                        st.warning("음성에서 인식된 문장이 없습니다.")
        else:
            st.info("현재 실행 중인 Streamlit 버전에서는 음성 입력 위젯을 지원하지 않습니다. 텍스트 답변을 이용해 주세요.")
    if st.session_state.answers:
        st.caption(f"완료한 답변 {len(st.session_state.answers)}개 · 제출 방식: {st.session_state.answers[-1]['mode']}")


def submit_answer(answer, mode):
    questions = st.session_state.get("questions", QUESTIONS)
    st.session_state.answers.append({"question": questions[st.session_state.question_index]["text"], "answer": answer, "mode": mode})
    if st.session_state.question_index == len(questions) - 1:
        st.session_state.show_result = True
        go("report")
    else:
        st.session_state.question_index += 1
        st.rerun()


def report():
    header()
    profile = st.session_state.profile
    text = " ".join(item["answer"] for item in st.session_state.answers)
    detail = min(92, 48 + len(re.findall(r"[가-힣A-Za-z0-9]", text)) // 4)
    readiness = min(94, 61 + len(st.session_state.answers) * 5 + (8 if profile.get("cover_letter") else 0))
    pass_rate = min(88, 54 + readiness // 5)
    st.markdown('<span class="pill">03 · 성장 확인</span>', unsafe_allow_html=True)
    st.markdown("## 오늘의 면접 리포트")
    st.caption(f"{datetime.now().strftime('%Y.%m.%d')} 기준 · {profile.get('company') or '관심 기업'} / {profile.get('role') or '관심 직무'}")
    a, b, c = st.columns(3)
    with a: st.markdown(f'<div class="metric"><b>{readiness}%</b>현재 준비도</div>', unsafe_allow_html=True)
    with b: st.markdown(f'<div class="metric"><b>{detail}%</b>답변 상세도</div>', unsafe_allow_html=True)
    with c: st.markdown(f'<div class="metric"><b>{pass_rate}%</b>예상 합격 가능성</div>', unsafe_allow_html=True)
    st.write("")
    st.markdown('<div class="insight"><b>EGG 코치의 한마디</b><br>경험의 재료는 충분히 모였어요. 답변마다 <b>내가 한 행동과 숫자로 보이는 결과</b>를 한 겹 더 얹으면 합격에 가까워져요.</div>', unsafe_allow_html=True)
    st.markdown("### 답변별 피드백")
    for number, item in enumerate(st.session_state.answers, 1):
        with st.expander(f"Q{number}. {item['question'][:45]}..."):
            st.write(item["answer"])
            st.info("질문의 의도에 맞게 경험을 연결했어요. 다음에는 결과를 수치로 구체화해 보세요.")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("다시 한 번 연습하기", use_container_width=True):
            st.session_state.question_index = 0
            st.session_state.answers = []
            go("interview")
    with col2:
        if st.button("프로필 보완하기", use_container_width=True):
            go("profile")


if st.session_state.page == "home":
    home()
elif st.session_state.page == "profile":
    profile()
elif st.session_state.page == "interview":
    interview()
else:
    report()
