# -*- coding: utf-8 -*-
"""자기소개서 파일에서 글자를 뽑아낸다.

지원 형식
  · PDF   : pypdf 로 페이지별 텍스트 추출
  · DOCX  : 표준 라이브러리만 사용(내부가 zip + XML 이라 그대로 읽는다)
  · HWPX  : 한글 최신 형식. 역시 zip + XML
  · HWP   : 한글 5.0 바이너리. olefile 로 BodyText 스트림을 읽어 기록을 해석
  · TXT   : 인코딩을 추정해 그대로 읽는다

업로드 파일은 신뢰할 수 없는 입력이므로 크기·압축 해제 용량·글자 수에 모두 상한을 둔다.
"""

import io
import os
import re
import struct
import zipfile
import zlib

MAX_UPLOAD = 10 * 1024 * 1024        # 업로드 파일 최대 10MB
MAX_UNPACKED = 60 * 1024 * 1024      # 압축을 풀었을 때 최대 60MB(압축 폭탄 방지)
MAX_CHARS = 20000                    # 자기소개서로 보관할 최대 글자 수
MAX_PDF_PAGES = 40

EXTENSIONS = (".pdf", ".docx", ".hwpx", ".hwp", ".txt")
ACCEPT_ATTRIBUTE = ".pdf,.docx,.hwpx,.hwp,.txt"

# 한글(HWP) 문단 텍스트 기록
HWPTAG_PARA_TEXT = 0x43
# 인라인 제어문자 중 뒤에 정보 8글자가 더 붙는 것들
HWP_EXTENDED_CONTROLS = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}


def _clean(text):
    """추출한 글자에서 제어문자를 없애고 공백을 정리한다."""
    text = (text or "").replace("\x00", " ")
    text = re.sub(r"[--]", " ", text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _xml_to_text(raw, paragraph_tags):
    """워드·한글 XML에서 문단 단위로 글자만 남긴다."""
    text = raw.decode("utf-8", "ignore")
    for tag in paragraph_tags:
        text = re.sub(r"</%s>" % tag, "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    for entity, character in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                              ("&quot;", '"'), ("&apos;", "'"), ("&#13;", "\n")):
        text = text.replace(entity, character)
    return text


def _safe_zip_read(data, wanted):
    """zip 안에서 필요한 파일만, 압축 해제 용량을 확인하며 읽는다."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None, "파일이 손상되었거나 지원하지 않는 형식입니다."

    with archive:
        if sum(info.file_size for info in archive.infolist()) > MAX_UNPACKED:
            return None, "파일 내용이 지나치게 큽니다. 자기소개서 부분만 따로 저장해 올려 주세요."

        names = archive.namelist()
        targets = [name for name in names if wanted(name)]
        if not targets:
            return None, "문서에서 본문을 찾지 못했습니다."

        chunks = []
        for name in sorted(targets):
            try:
                chunks.append(archive.read(name))
            except (zipfile.BadZipFile, RuntimeError):
                continue
        return chunks, None


# ========== 형식별 추출 ==========
def from_docx(data):
    chunks, error = _safe_zip_read(data, lambda name: name == "word/document.xml")
    if error:
        return None, error
    return _xml_to_text(chunks[0], ["w:p"]), None


def from_hwpx(data):
    chunks, error = _safe_zip_read(
        data, lambda name: name.startswith("Contents/section") and name.endswith(".xml"))
    if error:
        return None, error
    return "\n".join(_xml_to_text(chunk, ["hp:p", "p"]) for chunk in chunks), None


def from_pdf(data):
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, "PDF를 읽는 기능이 설치되지 않았습니다. (pip install pypdf) 텍스트로 붙여넣어 주세요."

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")                   # 빈 비밀번호로 열리는 경우만 허용
            except Exception:
                return None, "암호가 걸린 PDF입니다. 암호를 풀고 다시 올려 주세요."

        pages = reader.pages[:MAX_PDF_PAGES]
        text = "\n".join((page.extract_text() or "") for page in pages)
    except Exception:
        return None, "PDF를 읽지 못했습니다. 다른 형식으로 저장해 다시 시도해 주세요."

    if not text.strip():
        return None, "PDF에서 글자를 찾지 못했습니다. 스캔한 이미지 PDF라면 텍스트로 붙여넣어 주세요."
    return text, None


def _decompress_limited(data, limit):
    """압축을 풀되 정해진 크기까지만 만들어 낸다.

    작은 파일이 수 GB로 부풀어 서버 메모리를 고갈시키는 것(압축 폭탄)을 막는다.
    한도를 넘으면 None을 돌려준다.
    """
    if limit <= 0:
        return None
    engine = zlib.decompressobj(-15)
    try:
        result = engine.decompress(data, limit)
        if engine.unconsumed_tail:                   # 아직 남았다면 한도를 넘긴 것이다
            return None
    except zlib.error:
        return None
    return result


def _hwp_paragraph(payload):
    """HWP 문단 기록(UTF-16LE + 인라인 제어문자)에서 글자만 뽑는다."""
    letters = []
    position = 0
    while position + 1 < len(payload):
        code = payload[position] | (payload[position + 1] << 8)
        if code in HWP_EXTENDED_CONTROLS:
            position += 16                       # 제어문자 1개 + 정보 7개 = 8글자
            continue
        if code < 32:
            letters.append("\n" if code in (10, 13) else " ")
            position += 2
            continue
        letters.append(chr(code))
        position += 2
    return "".join(letters)


def _hwp_records(stream):
    """HWP 스트림을 기록 단위로 훑으며 문단 텍스트만 모은다."""
    parts = []
    position = 0
    size = len(stream)
    while position + 4 <= size:
        header = struct.unpack_from("<I", stream, position)[0]
        position += 4
        tag = header & 0x3FF
        length = (header >> 20) & 0xFFF
        if length == 0xFFF:                      # 길이가 크면 다음 4바이트에 실제 길이가 온다
            if position + 4 > size:
                break
            length = struct.unpack_from("<I", stream, position)[0]
            position += 4
        if length < 0 or position + length > size:
            break
        if tag == HWPTAG_PARA_TEXT:
            parts.append(_hwp_paragraph(stream[position:position + length]))
        position += length
    return "\n".join(parts)


def from_hwp(data):
    try:
        import olefile
    except ImportError:
        return None, "HWP를 읽는 기능이 설치되지 않았습니다. (pip install olefile) PDF로 저장해 올려 주세요."

    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except Exception:
        return None, "HWP 파일을 열지 못했습니다. 한글에서 다시 저장해 주세요."

    try:
        if not ole.exists("FileHeader"):
            return None, "한글 문서 형식이 아닙니다. (HWP 5.0 이상만 지원)"

        header = ole.openstream("FileHeader").read()
        if len(header) < 40 or not header.startswith(b"HWP Document File"):
            return None, "한글 문서 형식이 아닙니다. (HWP 5.0 이상만 지원)"
        compressed = bool(struct.unpack_from("<I", header, 36)[0] & 0x01)

        sections = sorted(name for entry in ole.listdir()
                          for name in ["/".join(entry)]
                          if name.startswith("BodyText/Section"))
        if not sections:
            return None, "문서에서 본문을 찾지 못했습니다."

        parts = []
        unpacked = 0
        for section in sections:
            raw = ole.openstream(section).read()
            if compressed:
                raw = _decompress_limited(raw, MAX_UNPACKED - unpacked)
                if raw is None:
                    break                            # 압축을 풀면 상한을 넘는 파일은 여기서 멈춘다
            unpacked += len(raw)
            if unpacked > MAX_UNPACKED:
                break
            parts.append(_hwp_records(raw))
    finally:
        ole.close()

    text = "\n".join(parts)
    if not text.strip():
        return None, "HWP에서 글자를 찾지 못했습니다. PDF로 저장해 다시 올려 주세요."
    return text, None


def from_txt(data):
    for encoding in ("utf-8", "cp949", "utf-16"):
        try:
            return data.decode(encoding), None
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "ignore"), None


READERS = {
    ".pdf": from_pdf,
    ".docx": from_docx,
    ".hwpx": from_hwpx,
    ".hwp": from_hwp,
    ".txt": from_txt,
}


def extract(upload):
    """업로드된 자기소개서 파일에서 글자를 뽑는다. 성공하면 (텍스트, None)."""
    filename = (getattr(upload, "filename", "") or "").strip()
    if not filename:
        return None, "파일을 선택해 주세요."

    extension = os.path.splitext(filename)[1].lower()
    if extension not in READERS:
        return None, "PDF, DOCX, HWP, HWPX, TXT 파일만 올릴 수 있어요."

    data = upload.read(MAX_UPLOAD + 1)
    if not data:
        return None, "파일이 비어 있습니다."
    if len(data) > MAX_UPLOAD:
        return None, "파일이 너무 큽니다. %dMB 이하로 올려 주세요." % (MAX_UPLOAD // 1024 // 1024)

    # 확장자만 믿지 않고 실제 내용도 확인한다
    if extension in (".docx", ".hwpx") and not data.startswith(b"PK"):
        return None, "파일 형식이 확장자와 다릅니다. 원본 문서를 다시 저장해 올려 주세요."
    if extension == ".pdf" and not data.startswith(b"%PDF"):
        return None, "PDF 파일이 아닙니다. 원본 문서를 다시 저장해 올려 주세요."

    text, error = READERS[extension](data)
    if error:
        return None, error

    text = _clean(text)
    if len(text) < 20:
        return None, "문서에서 읽어낸 내용이 너무 짧습니다. 직접 붙여넣어 주세요."

    truncated = len(text) > MAX_CHARS
    return {"text": text[:MAX_CHARS], "chars": len(text[:MAX_CHARS]),
            "truncated": truncated, "filename": filename}, None
