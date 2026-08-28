# -*- coding: utf-8 -*-
"""면접 답변 평가 로직.

두 가지 채점표(rubric)를 쓴다.
  · 음성 답변: 내용(질문 적합도·구체성)에 더해 전달력(발음/속도·목소리 크기·안정감·길이)까지 본다.
  · 텍스트 답변: 전달력 대신 글로만 확인할 수 있는 문장 가독성과 분량을 본다.
두 채점표는 '질문 적합도'와 '내용 구체성'을 공통 축으로 공유하되 가중치가 다르다.

측정 신뢰도를 함께 다룬다. 녹음이 너무 짧거나 유성음이 부족해 지표를 믿을 수 없으면
임의로 점수를 매기지 않고 '측정 불가'로 표시한 뒤 나머지 항목으로 가중치를 재분배한다.
"""

import re

# ========== 채점표 ==========
# (키, 항목명, 가중치)
VOICE_RUBRIC = [
    ("relevance", "질문 적합도", 25),
    ("substance", "내용 구체성", 20),
    ("delivery", "발음·전달 속도", 20),
    ("steadiness", "목소리 안정감", 13),
    ("volume", "목소리 전달력", 12),
    ("duration", "답변 길이", 10),
]

TEXT_RUBRIC = [
    ("relevance", "질문 적합도", 30),
    ("substance", "내용 구체성", 30),
    ("readability", "문장 가독성", 20),
    ("length", "분량 적정성", 20),
]

LABELS = {key: label for key, label, _ in VOICE_RUBRIC + TEXT_RUBRIC}
SHARED_KEYS = {"relevance", "substance"}

# ---------- 측정 신뢰도 기준 ----------
MIN_SPEECH_FRAMES = 40      # 2초 이상 발화가 있어야 목소리 크기를 판단한다
MIN_VOICED_PAIRS = 30       # 1.5초 이상 유성음이 있어야 떨림을 판단한다
MIN_SYLLABLES = 25          # 이보다 짧으면 말하기 속도를 신뢰할 수 없다

# ---------- 음성 기준값 ----------
IDEAL_SNR = 22.0            # 배경소음 대비 목소리 크기(dB). 기기 감도와 무관하게 비교된다
RATE_RANGE = (4.4, 7.2)     # 침묵을 뺀 실제 발화 속도(음절/초) 권장 구간
TIME_RANGE = (40.0, 95.0)   # 답변 길이 권장 구간(초)
IDEAL_SECONDS = 65.0

# ---------- 텍스트 기준값 ----------
CHAR_RANGE = (320, 750)     # 면접 답변 권장 분량(글자)
SENTENCE_RANGE = (25, 70)   # 문장당 권장 길이(글자)

FILLERS = ["음", "어어", "그니까", "그러니까", "뭐랄까", "저기", "약간", "이제", "인제", "뭐지", "그게", "막상"]
WRITTEN_FILLERS = ["그냥", "되게", "엄청", "약간", "뭔가", "같아요", "인 것 같습니다"]

# STAR 구조 신호어
SITUATION_WORDS = ("당시", "프로젝트", "인턴", "학회", "동아리", "팀", "과제", "대회", "아르바이트",
                   "학기", "회사", "업무", "상황", "현장", "고객", "사용자")
ACTION_WORDS = ("제가", "직접", "맡아", "맡았", "주도", "제안", "설계", "분석", "기획", "개발", "정리",
                "설득", "조율", "협업", "구축", "실행", "개선했", "만들었", "진행했", "도입")
RESULT_WORDS = ("결과", "성과", "개선", "향상", "달성", "절감", "상승", "증가", "감소", "전환율",
                "매출", "만족도", "수상", "선정", "단축")
REASON_WORDS = ("때문", "이유", "그래서", "따라서", "위해", "덕분", "계기")

QUANTITY = re.compile(r"\d+\s*(?:%|퍼센트|배|위|명|건|개|억|만원|천만|주|개월|시간|일|점|등)"
                      r"|[일이삼사오육칠팔구십백천만]+\s*(?:퍼센트|배|위|명|건|개월|시간)")

# 조사를 떼어 어간만 비교하기 위한 접미사
JOSA = ("에서는", "에서", "으로", "에게", "까지", "부터", "이라", "라고", "하고", "이나", "든지", "처럼",
        "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "도", "로", "만", "께")

STOPWORDS = {"본인", "경험", "설명", "이유", "무엇", "어떤", "그것", "우리", "저희", "때문", "정도",
             "과정", "생각", "사람", "이것", "부분", "가장", "대해", "관련", "위해", "통해", "말씀",
             "주세", "주세요", "해주", "합니", "습니", "니다", "있는", "하는", "된다", "그리고",
             "하지만", "그래서", "저는", "제가", "많은", "매우", "정말"}

# 질문 유형별로 반드시 담겨야 하는 요소
TAG_EXPECTATIONS = {
    "지원동기": (REASON_WORDS, "지원 이유를 '~때문에', '~을 위해'처럼 분명한 연결어로 밝혀 주세요."),
    "직무역량": (("역량", "강점", "능력", "기술", "스킬", "전문성"), "어떤 역량인지 이름을 먼저 말하고 근거를 붙여 주세요."),
    "문제해결": (("갈등", "문제", "충돌", "해결", "조율", "설득", "합의", "대안"), "문제 상황과 해결 행동을 함께 담아 주세요."),
    "자격·전문성": (("자격", "활용", "업무", "적용", "공부", "학습", "준비", "대신", "보완"),
                "자격이나 지식을 실제 업무에 어떻게 쓸지 연결해 주세요."),
    "어학·글로벌": (("영어", "외국어", "회화", "번역", "해외", "글로벌", "공부", "학습", "계획", "보완"),
                "어학 활용 경험이나 앞으로의 계획을 구체적으로 말해 주세요."),
    "대외활동": (("활동", "프로젝트", "동아리", "인턴", "역할", "맡", "참여", "준비", "공부"),
              "어떤 활동에서 무슨 역할을 했는지 밝혀 주세요."),
    "전공·기초": (("전공", "수업", "과목", "공부", "학습", "지식", "적용", "프로젝트"),
              "배운 내용을 직무와 연결해 설명해 주세요."),
    "포부": (("목표", "계획", "기여", "성장", "년", "하고 싶", "만들"),
           "시점과 목표를 구체적인 숫자로 말하면 좋습니다."),
    "강점·보완": (("강점", "장점", "약점", "보완", "노력", "개선"),
               "강점과 보완점을 각각 하나씩, 근거와 함께 말해 주세요."),
    "협업": (("팀", "동료", "협업", "소통", "대화", "조율", "이해"),
           "감정보다 구체적인 방법을 말해 주세요."),
    "성실성": (("이유", "때문", "당시", "집중", "병행", "아르바이트", "준비", "이후", "개선", "노력", "학기"),
             "낮은 학점의 배경을 사실대로 밝히고, 그 시기에 집중한 일과 이후 변화를 함께 말해 주세요."),
    "압박": (("부족", "약", "보완", "개선", "노력", "앞으로", "계획"),
           "약점을 인정하고 메울 방법까지 함께 말해 주세요."),
}


def _clamp(value, low=0.0, high=100.0):
    return max(low, min(high, value))


def _stem(token):
    for josa in JOSA:
        if len(token) > len(josa) + 1 and token.endswith(josa):
            return token[: -len(josa)]
    return token


def _keywords(text):
    """한글·영문 토큰에서 조사를 떼고 의미 있는 단어만 남긴다."""
    words = re.findall(r"[가-힣]{2,}|[A-Za-z]{3,}|\d+", text or "")
    result = []
    for word in words:
        stem = _stem(word.lower())
        if len(stem) < 2 or stem in STOPWORDS:
            continue
        if stem not in result:
            result.append(stem)
    return result


def _hangul_count(text):
    return len(re.findall(r"[가-힣]", text or ""))


def _sentences(text):
    return [part.strip() for part in re.split(r"[.!?\n]+", text or "") if part.strip()]


def _number(value, default=None):
    """측정값을 숫자로 바꾼다. 숫자가 아니면 기본값을 돌려준다(잘못된 입력 방어)."""
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


def _item(score, comment, measured=True, reason=None):
    return {"score": None if score is None else int(round(score)),
            "comment": comment, "measured": measured, "reason": reason}


# ========== 공통 항목 ==========
def score_relevance(question, answer, profile=None, tag=None):
    """질문 적합도: 질문·직무 키워드 반영도와 질문 유형별 필수 요소를 함께 본다."""
    profile = profile or {}
    keywords = _keywords(question)
    for extra in (profile.get("role"), profile.get("company")):
        for word in _keywords(extra or ""):
            if word not in keywords:
                keywords.append(word)

    answer_stems = _keywords(answer)
    matched = 0
    for keyword in keywords:
        for stem in answer_stems:
            if keyword in stem or stem in keyword or (len(keyword) > 2 and keyword[:2] == stem[:2]):
                matched += 1
                break
    coverage = matched / len(keywords) if keywords else 0.6

    score = 34.0 + coverage * 66.0

    missing_expectation = None
    if tag in TAG_EXPECTATIONS:                         # 질문 유형별 필수 요소 확인
        words, advice = TAG_EXPECTATIONS[tag]
        if not any(word in (answer or "") for word in words):
            score -= 12
            missing_expectation = advice

    length = _hangul_count(answer)
    if length < 40:
        score = min(score, 50)                          # 근거를 확인할 수 없을 만큼 짧은 답변
    score = _clamp(score, 10, 100)

    off_topic = coverage < 0.22 and length >= 40
    if off_topic:
        comment = "질문의 핵심 단어가 답변에 거의 등장하지 않습니다. 질문을 되짚고 결론부터 말해 보세요."
    elif missing_expectation:
        comment = missing_expectation
    elif coverage < 0.45:
        comment = "질문과 방향은 맞지만 연결이 옅어요. 질문 속 단어를 그대로 받아 답을 시작해 보세요."
    else:
        comment = "질문 의도에 맞게 경험을 연결했어요."
    return _item(score, comment), off_topic


def score_substance(answer, strict=False):
    """내용 구체성: 상황·행동·결과(STAR)와 숫자 근거가 담겼는지.

    strict=True(텍스트 답변)는 고쳐 쓸 수 있는 조건이므로 같은 기준을 조금 더 엄격히 적용한다.
    """
    text = answer or ""
    has_situation = any(word in text for word in SITUATION_WORDS)
    has_action = any(word in text for word in ACTION_WORDS)
    has_result = any(word in text for word in RESULT_WORDS)
    quantities = len(QUANTITY.findall(text))

    score = 42.0
    score += 14 if has_situation else 0
    score += 18 if has_action else 0
    score += 16 if has_result else 0
    score += min(12, quantities * 6)

    if strict:
        score -= 8                                       # 글은 다듬을 수 있으므로 기준을 높인다
        if quantities == 0:
            score -= 6
    score = _clamp(score, 12, 100)

    missing = []
    if not has_situation:
        missing.append("어떤 상황이었는지")
    if not has_action:
        missing.append("본인이 한 행동이")
    if not has_result:
        missing.append("그래서 어떻게 됐는지가")

    if missing:
        comment = " · ".join(missing) + " 빠졌어요. 상황·행동·결과 순서로 채워 보세요."
    elif quantities == 0:
        comment = "구성은 좋아요. 결과를 숫자로 바꾸면 설득력이 한 단계 올라갑니다."
    else:
        comment = "상황·행동·결과가 숫자 근거와 함께 잘 담겼어요."
    return _item(score, comment)


# ========== 음성 전용 항목 ==========
def score_volume(metrics):
    """목소리 전달력: 배경소음 대비 목소리 크기(SNR)로 본다.

    절대 음량(dBFS)은 마이크·기기마다 값이 달라 비교가 어렵다.
    같은 녹음 안의 소음 바닥을 기준으로 삼으면 기기 차이가 상쇄된다.
    """
    snr = metrics.get("snr_db")
    frames = _number(metrics.get("speech_frames"), 0)
    if snr is None or frames < MIN_SPEECH_FRAMES:
        return _item(None, "발화 구간이 짧아 목소리 크기를 판단하지 않았어요.", False, "발화 2초 미만")

    snr = _number(snr)
    if snr is None:
        return _item(None, "목소리 크기를 판단하지 않았어요.", False, "측정값 없음")
    score = 100.0 - max(0.0, IDEAL_SNR - snr) * 3.4          # 소음에 묻힐수록 감점
    score -= _number(metrics.get("dropout_ratio"), 0) * 55   # 문장 끝에서 목소리가 꺼지는 구간
    score -= _number(metrics.get("loud_ratio"), 0) * 70      # 마이크가 깨질 만큼 큰 구간
    score = _clamp(score, 12, 100)

    if snr < 10:
        comment = "주변 소음과 목소리 크기가 비슷해 잘 들리지 않아요. 조용한 곳에서 한 톤 올려 말해 보세요."
    elif snr < 16:
        comment = "목소리가 다소 묻힙니다. 마이크를 조금 가까이 두고 배에 힘을 주어 말해 보세요."
    elif _number(metrics.get("loud_ratio"), 0) > 0.04:
        comment = "소리가 커서 일부 구간이 찢어졌어요. 마이크와 거리를 조금 두는 편이 좋습니다."
    elif _number(metrics.get("dropout_ratio"), 0) > 0.22:
        comment = "문장 끝에서 목소리가 흐려집니다. 마지막 어미까지 같은 크기로 마무리해 보세요."
    else:
        comment = "면접관이 편하게 들을 수 있는 성량이었어요."
    return _item(score, comment)


def score_steadiness(metrics):
    """목소리 안정감: 음높이 흔들림(jitter)·세기 흔들림(shimmer)·음높이 산포를 함께 본다."""
    jitter = metrics.get("jitter")
    pairs = _number(metrics.get("voiced_pairs"), 0)
    if jitter is None or pairs < MIN_VOICED_PAIRS:
        return _item(None, "유성음 구간이 부족해 떨림을 판단하지 않았어요.", False, "유성음 1.5초 미만")

    jitter = _number(jitter)
    if jitter is None:
        return _item(None, "떨림을 판단하지 않았어요.", False, "측정값 없음")
    shimmer = _number(metrics.get("shimmer"), 0)
    spread = _number(metrics.get("pitch_spread"), 0)

    score = 100.0
    score -= max(0.0, jitter - 0.05) * 300               # 음높이가 프레임마다 튀는 정도
    score -= max(0.0, shimmer - 0.14) * 130              # 목소리 세기가 떨리는 정도
    score -= max(0.0, spread - 0.38) * 55                # 음높이가 전반적으로 널뛰는 정도
    score = _clamp(score, 15, 100)

    if jitter > 0.20 or shimmer > 0.32:
        comment = "목소리 떨림이 뚜렷합니다. 첫 문장 전에 숨을 한 번 고르고 천천히 시작해 보세요."
    elif jitter > 0.12 or shimmer > 0.24:
        comment = "군데군데 목소리가 흔들려요. 문장을 짧게 끊어 말하면 훨씬 안정적으로 들립니다."
    elif spread > 0.45:
        comment = "톤의 높낮이 변화가 큽니다. 한 문장 안에서는 톤을 유지해 보세요."
    else:
        comment = "떨림 없이 안정적인 목소리를 유지했어요."
    return _item(score, comment)


def score_delivery(metrics, answer):
    """발음·전달 속도.

    브라우저 받아쓰기 신뢰도는 기기·브라우저마다 값이 달라 보조 지표로만 쓰고,
    기기와 무관한 발화 속도·군말·긴 침묵·고역 에너지 비율을 주 지표로 삼는다.
    """
    syllables = _hangul_count(answer)
    duration = _number(metrics.get("duration"), 0)
    if syllables < MIN_SYLLABLES or duration < 8:
        return _item(None, "발화량이 적어 발음·속도를 판단하지 않았어요.", False, "발화 25음절 미만")

    speech_ratio = metrics.get("speech_ratio")
    speaking = duration * _number(speech_ratio, 0.75) if speech_ratio else duration * 0.75
    speaking = max(1.0, speaking)
    rate = syllables / speaking

    score = 92.0
    low, high = RATE_RANGE
    if rate > high:
        score -= (rate - high) * 11                      # 빨라서 끝음절이 뭉개짐
    elif rate < low:
        score -= (low - rate) * 9                        # 느려서 늘어짐

    fillers = sum((answer or "").count(word) for word in FILLERS)
    score -= min(16, (fillers / max(1.0, syllables / 100)) * 4)

    pause_ratio = _number(metrics.get("long_pause_ratio"), 0)
    score -= min(14, pause_ratio * 45)                   # 1.5초 이상 침묵이 잦은 경우

    hf_ratio = metrics.get("hf_ratio")
    if hf_ratio is not None:
        # 자음이 뭉개지면 2kHz 이상 에너지 비율이 낮아진다. 마이크 특성 영향이 있어 폭을 좁게 둔다
        score -= min(10, max(0.0, 0.10 - _number(hf_ratio, 0.10)) * 90)

    confidence = metrics.get("confidence")
    samples = metrics.get("confidence_samples") or 0
    if confidence and _number(samples, 0) >= 3:
        score += _clamp((_number(confidence, 0.78) - 0.78) * 40, -8, 8)   # 보조 지표: 최대 ±8점

    score = _clamp(score, 15, 100)

    if rate > high:
        comment = "말이 빨라 끝음절이 뭉개집니다. 한 문장이 끝날 때마다 반 박자 쉬어 보세요."
    elif rate < low:
        comment = "전달 속도가 느려 답변이 늘어져요. 핵심 문장은 조금 더 붙여서 말해 보세요."
    elif fillers >= 4:
        comment = "'음, 그니까' 같은 군말이 잦아요. 잠깐 멈추는 편이 더 또렷하게 들립니다."
    elif pause_ratio > 0.18:
        comment = "말 중간의 긴 침묵이 많아요. 생각을 정리한 뒤 문장을 시작해 보세요."
    else:
        comment = "속도와 발음 모두 또렷하게 전달됐어요."
    return _item(score, comment)


def score_duration(metrics, answer):
    """답변 길이(시간): 권장 40~95초 대비 적절했는지, 침묵이 과하지 않았는지."""
    duration = _number(metrics.get("duration"), 0)
    if duration <= 0:
        return _item(None, "녹음 길이를 확인하지 못했어요.", False, "길이 정보 없음")

    low, high = TIME_RANGE
    if duration < low:
        score = 100 - (low - duration) * 1.6
        comment = "답변이 짧아요. 1분 내외로 상황·행동·결과를 모두 담아 보세요."
    elif duration > high:
        score = 100 - (duration - high) * 0.9
        comment = "답변이 길어 면접관의 집중이 흐려질 수 있어요. 1분 30초 안에 마무리해 보세요."
    else:
        score = 100 - abs(duration - IDEAL_SECONDS) * 0.35
        comment = "권장 시간 안에서 알맞게 답했어요."

    speech_ratio = metrics.get("speech_ratio")
    if speech_ratio is not None and _number(speech_ratio, 1.0) < 0.45 and duration > 20:
        score -= 12
        comment = "녹음 시간의 절반 이상이 침묵이었어요. 준비가 된 뒤 녹음을 시작해 보세요."
    return _item(_clamp(score, 15, 100), comment)


# ========== 텍스트 전용 항목 ==========
def score_readability(answer):
    """문장 가독성: 문장 길이 분포, 어미 반복, 구어체 군더더기."""
    text = answer or ""
    sentences = _sentences(text)
    if not sentences:
        return _item(20, "문장을 구분할 수 없어요. 마침표로 문장을 끊어 주세요.")

    lengths = [_hangul_count(sentence) for sentence in sentences]
    average = sum(lengths) / len(lengths)
    too_long = sum(1 for length in lengths if length > 90)

    score = 92.0
    low, high = SENTENCE_RANGE
    if average > high:
        score -= (average - high) * 0.7                  # 한 문장에 여러 내용이 섞임
    elif average < low:
        score -= (low - average) * 0.8                   # 지나치게 끊겨 흐름이 약함
    score -= too_long * 6

    endings = [sentence[-3:] for sentence in sentences if len(sentence) >= 3]
    monotone = False
    if len(endings) >= 3:
        repeat = max(endings.count(ending) for ending in set(endings)) / len(endings)
        if repeat > 0.75:
            score -= 10                                  # 같은 어미만 반복돼 단조로움
            monotone = True

    spoken = sum(text.count(word) for word in WRITTEN_FILLERS)
    score -= min(14, spoken * 3.5)
    score = _clamp(score, 15, 100)

    if average > high:
        comment = "한 문장이 길어 읽기 어려워요. 한 문장에 한 가지 내용만 담아 보세요."
    elif average < low:
        comment = "문장이 너무 짧게 끊겨 흐름이 약해요. 이유와 결과를 이어 붙여 보세요."
    elif spoken >= 3:
        comment = "'그냥, 뭔가' 같은 구어체 표현이 잦아요. 문어체로 다듬으면 신뢰감이 올라갑니다."
    elif monotone:
        comment = "문장 끝맺음이 단조로워요. 어미를 바꿔 가며 리듬을 주면 잘 읽힙니다."
    else:
        comment = "문장 길이와 표현이 읽기 좋게 정리돼 있어요."
    return _item(score, comment)


def score_length_text(answer):
    """분량 적정성: 실제 면접에서 1분가량 말할 수 있는 분량인지."""
    chars = _hangul_count(answer)
    low, high = CHAR_RANGE

    if chars < low:
        score = 100 - (low - chars) * 0.16
        comment = "분량이 부족해 근거가 얇아 보여요. 경험을 한 가지 더 붙여 %d자 이상으로 채워 보세요." % low
    elif chars > high:
        score = 100 - (chars - high) * 0.06
        comment = "내용이 길어 핵심이 묻힙니다. 결론 문장을 앞으로 빼고 %d자 안으로 줄여 보세요." % high
    else:
        score = 100 - abs(chars - (low + high) / 2) * 0.03
        comment = "1분 답변에 알맞은 분량이에요."
    return _item(_clamp(score, 20, 100), comment)


# ========== 종합 ==========
def evaluate_answer(question, answer, metrics=None, profile=None, tag=None):
    """답변 하나를 채점표에 따라 평가한다. metrics가 있으면 음성 채점표를 쓴다."""
    metrics = metrics or None
    is_voice = bool(metrics)
    rubric = VOICE_RUBRIC if is_voice else TEXT_RUBRIC

    relevance, off_topic = score_relevance(question, answer, profile, tag)
    computed = {
        "relevance": relevance,
        "substance": score_substance(answer, strict=not is_voice),
    }
    if is_voice:
        computed["delivery"] = score_delivery(metrics, answer)
        computed["steadiness"] = score_steadiness(metrics)
        computed["volume"] = score_volume(metrics)
        computed["duration"] = score_duration(metrics, answer)
    else:
        computed["readability"] = score_readability(answer)
        computed["length"] = score_length_text(answer)

    items = []
    for key, label, weight in rubric:
        detail = dict(computed[key])
        detail.update({"key": key, "label": label, "weight": weight, "shared": key in SHARED_KEYS})
        items.append(detail)

    measured = [item for item in items if item["measured"]]
    measured_weight = sum(item["weight"] for item in measured)
    total = round(sum(item["score"] * item["weight"] for item in measured) / measured_weight)
    if off_topic:
        total = min(total, 58)          # 질문을 벗어난 답변은 다른 항목이 좋아도 통과하기 어렵다

    reliability = measured_weight / sum(item["weight"] for item in items)
    weakest = min(measured, key=lambda item: item["score"])
    return {
        "mode": "음성" if is_voice else "텍스트",
        "rubric": "음성 채점표" if is_voice else "텍스트 채점표",
        "total": total,
        "grade": grade_of(total),
        "pass_rate": pass_rate_from(total, profile, reliability),
        "items": items,
        "off_topic": off_topic,
        "headline": weakest["comment"],
        "reliability": round(reliability, 2),
        "unmeasured": [item["label"] for item in items if not item["measured"]],
    }


def pass_rate_from(total, profile=None, reliability=1.0):
    """총점을 예상 합격률로 환산한다. 측정하지 못한 항목이 많으면 보수적으로 낮춘다."""
    rate = total * 0.82 + 4
    if profile and profile.get("cover_letter"):
        rate += 3
    if profile and profile.get("certificates"):
        rate += 2
    if reliability < 0.8:
        rate -= (0.8 - reliability) * 25
    return int(_clamp(round(rate), 12, 94))


def summarize(answers, profile=None):
    """전체 답변의 총점·항목 평균·예상 합격률과 제출 방식별 결과를 만든다."""
    scored = [answer["evaluation"] for answer in answers if answer.get("evaluation")]
    if not scored:
        return None

    total = round(sum(item["total"] for item in scored) / len(scored))
    buckets = {}
    for evaluation in scored:
        for item in evaluation["items"]:
            if item["measured"]:
                buckets.setdefault(item["key"], []).append(item["score"])

    order = [key for key, _, _ in VOICE_RUBRIC]
    order += [key for key, _, _ in TEXT_RUBRIC if key not in order]

    items = []
    for key in order:
        if key not in buckets:
            continue
        average = round(sum(buckets[key]) / len(buckets[key]))
        items.append({
            "key": key,
            "label": LABELS[key],
            "score": average,
            "shared": key in SHARED_KEYS,
            "count": len(buckets[key]),
            "grade": grade_of(average),
        })

    voice = [item for item in scored if item["mode"] == "음성"]
    text = [item for item in scored if item["mode"] == "텍스트"]
    reliability = sum(item["reliability"] for item in scored) / len(scored)

    return {
        "total": total,
        "grade": grade_of(total),
        "pass_rate": pass_rate_from(total, profile, reliability),
        "items": items,
        "voice_count": len(voice),
        "text_count": len(text),
        "voice_avg": round(sum(item["total"] for item in voice) / len(voice)) if voice else None,
        "text_avg": round(sum(item["total"] for item in text) / len(text)) if text else None,
        "off_topic_count": sum(1 for item in scored if item["off_topic"]),
        "unmeasured": sorted({label for item in scored for label in item["unmeasured"]}),
        "best": max(items, key=lambda item: item["score"]) if items else None,
        "worst": min(items, key=lambda item: item["score"]) if items else None,
    }


def grade_of(score):
    if score >= 90:
        return "A+"
    if score >= 82:
        return "A"
    if score >= 74:
        return "B+"
    if score >= 66:
        return "B"
    if score >= 58:
        return "C+"
    if score >= 50:
        return "C"
    return "D"
