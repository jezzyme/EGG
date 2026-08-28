# -*- coding: utf-8 -*-
"""프로필 입력값 정리.

경력·자격 칸은 비워 두거나 'X', '-', '없습니다'처럼 제각각 적히기 쉽다.
이런 값은 모두 '없음' 하나로 맞춰야 질문을 만들 때 "적지 않은 항목"을 정확히 가려낼 수 있다.

특수문자가 섞이면(예: '없음.') 칸 아래에 작은 안내를 띄우고, 값은 '없음'으로 정리한다.
"""

import re

NONE_TEXT = "없음"

# '없다'는 뜻으로 흔히 적는 표현들(공백·특수문자를 뗀 뒤 비교한다)
NONE_WORDS = {
    "없음", "없습니다", "없어요", "없슴", "없다", "없었음", "없", "무",
    "해당없음", "해당사항없음", "해당무", "미보유", "미해당", "비해당",
    "x", "xx", "o", "n", "na", "none", "no", "nothing", "null", "nil", "0",
}

# 자격증·어학처럼 실제로 쓰이는 구분 기호는 허용한다(예: "ADsP, SQLD", "TOEIC 890 (2024)")
ALLOWED = re.compile(r"^[가-힣a-zA-Z0-9\s,./·()%+\-]*$")

# 글자·숫자만 남긴다(비교용)
ONLY_WORD = re.compile(r"[^가-힣a-zA-Z0-9]")

# 질문을 만드는 데 쓰는 칸: (필드명, 화면 이름)
OPTIONAL_FIELDS = [
    ("department", "전공 / 학과"),
    ("double_major", "복수 · 부 · 융합전공"),
    ("certificates", "자격증"),
    ("language", "어학 성적"),
    ("activities", "인턴 · 대외활동 · 수상"),
]

# 인적사항으로만 받고 질문에는 절대 쓰지 않는 칸(블라인드 면접 원칙)
PERSONAL_FIELDS = [
    ("school", "학교"),
]

# 여러 개를 쉼표로 나눠 적는 칸
MULTI_FIELDS = ("certificates", "activities", "double_major")

# 학점이 이 값 이하이면(4.5 만점 환산) 성실성 관련 질문을 덧붙인다
LOW_GPA = 3.5
DEFAULT_SCALE = 4.5
GPA_SCALES = (4.0, 4.3, 4.5, 5.0, 100.0)

SPECIAL_HINT = "특수문자는 입력할 수 없어요. 한글·영문·숫자로 적어 주세요."
NONE_HINT = "경험이 없으면 특수문자 없이 '없음'이라고만 적어 주세요."


def is_none_value(value):
    """'없다'는 뜻으로 적은 값인지 판단한다."""
    if not (value or "").strip():
        return False                                # 빈 값은 clean_field 에서 따로 처리한다
    stripped = ONLY_WORD.sub("", value).lower()
    if not stripped:
        return True                                 # '-', '/', '.' 처럼 기호만 적은 경우
    return stripped in NONE_WORDS


def is_blank_or_none(value):
    """비었거나 '없음'인지(질문을 만들 때 '적지 않은 항목'으로 볼지)."""
    value = (value or "").strip()
    return not value or is_none_value(value)


QUOTES = "'\"‘’“”`"


def strip_quotes(value):
    """'ADsP', "컴활 2급" 처럼 따옴표를 붙여 적어도 그대로 받아들인다."""
    text = (value or "")
    for quote in QUOTES:
        text = text.replace(quote, "")
    return text


def clean_field(value):
    """칸 하나를 정리한다. (정리된 값, 안내 문구 또는 None)"""
    value = strip_quotes(value).strip()
    if not value:
        return NONE_TEXT, None                      # 비워 두면 '없음'으로 채운다

    if is_none_value(value):
        # '없음.' 처럼 특수문자가 붙었으면 알려 주고 '없음'으로 맞춘다
        hint = SPECIAL_HINT if ONLY_WORD.search(value.replace(" ", "")) else None
        return NONE_TEXT, hint

    if not ALLOWED.match(value):
        return value, SPECIAL_HINT                  # 값은 그대로 두고 다시 입력받는다

    return re.sub(r"\s+", " ", value), None


def split_items(value):
    """쉼표(또는 ·, 줄바꿈)로 구분해 적은 항목을 하나씩 나눈다.

    예) "'ADsP', '사회조사분석사 2급', '컴퓨터활용능력 2급'" → 자격증 3개
    """
    if is_blank_or_none(value):
        return []
    parts = re.split(r"[,\n·/]+", strip_quotes(value))
    items = [re.sub(r"\s+", " ", part).strip(" .;") for part in parts]
    return [item for item in items if item and not is_none_value(item)]


def count_items(value):
    return len(split_items(value))


def describe_items(value):
    """'3개 · ADsP, 사회조사분석사 2급, 컴퓨터활용능력 2급' 형태의 안내 문구."""
    items = split_items(value)
    if not items:
        return None
    return "%d개로 인식했어요: %s" % (len(items), " · ".join(items))


def parse_gpa(value):
    """학점을 읽어 4.5 만점 기준으로 환산한다.

    '3.42', '3.42/4.5', '3.42 (4.5 만점)', '85/100' 처럼 적어도 인식한다.
    """
    text = strip_quotes(value).strip()
    if not text or is_none_value(text):
        return None

    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    if not numbers:
        return None

    score = float(numbers[0])
    scale = None
    for candidate in numbers[1:]:
        number = float(candidate)
        if number in GPA_SCALES:
            scale = number
            break
    if scale is None:
        if 5.0 < score < 40.0:
            return None                              # 만점을 안 적은 애매한 값(예: '9.9')은 되묻는다
        scale = 100.0 if score >= 40.0 else DEFAULT_SCALE
    if score > scale:
        return None                                  # 만점을 넘는 값은 잘못 적은 것으로 본다

    normalized = round(score / scale * DEFAULT_SCALE, 2)
    return {
        "raw": text,
        "score": score,
        "scale": scale,
        "normalized": normalized,                    # 4.5 만점 환산
        "low": normalized <= LOW_GPA,
    }


def clean_profile(form):
    """프로필 입력 전체를 정리한다. (정리된 값들, 칸별 안내, 다시 입력이 필요한지)"""
    cleaned = {}
    hints = {}
    blocking = False

    for name, _label in OPTIONAL_FIELDS + PERSONAL_FIELDS:
        value, hint = clean_field(form.get(name))
        cleaned[name] = value
        if hint:
            hints[name] = hint
            if not is_none_value(value):
                blocking = True                     # 특수문자가 섞인 실제 입력은 고쳐야 넘어간다

    # 학점은 숫자만 확인한다(형식이 틀리면 다시 입력받는다)
    gpa_raw = strip_quotes(form.get("gpa")).strip()
    cleaned["gpa"] = gpa_raw
    if gpa_raw and not is_none_value(gpa_raw):
        if parse_gpa(gpa_raw) is None:
            hints["gpa"] = "학점은 '3.42' 또는 '3.42/4.5'처럼 숫자로 적어 주세요."
            blocking = True
    elif is_none_value(gpa_raw):
        cleaned["gpa"] = NONE_TEXT

    # 기업·직무는 '없음'으로 채우지 않고 그대로 둔다(비어 있으면 기본 문구를 쓴다)
    for name in ("company", "role"):
        value = re.sub(r"\s+", " ", (form.get(name) or "").strip())
        if value and not ALLOWED.match(value):
            hints[name] = SPECIAL_HINT
            blocking = True
        cleaned[name] = value

    return cleaned, hints, blocking


def missing_fields(profile):
    """'없음'이거나 비어 있는 항목의 이름 목록."""
    return [name for name, _label in OPTIONAL_FIELDS if is_blank_or_none(profile.get(name))]


def filled_fields(profile):
    """실제로 내용이 적힌 항목의 이름 목록."""
    return [name for name, _label in OPTIONAL_FIELDS if not is_blank_or_none(profile.get(name))]
