# EGG 배포 안내 (무료 Render + 외부 PostgreSQL)

무료 호스팅은 **디스크가 배포·재시작마다 초기화**됩니다. 그래서 계정과 연습 기록은
앱과 분리된 외부 데이터베이스에 저장합니다. 이렇게 해 두면 나중에 유료로 올려도 코드를 고칠 필요가 없습니다.

```
[사용자] → [Render 무료 웹서비스 : Flask]  →  [외부 PostgreSQL : 계정·연습 기록]
                (재시작되면 초기화)              (그대로 남음)
```

로컬 개발은 설정 없이 지금처럼 SQLite 파일(`egg.db`)로 동작합니다.

---

## 1단계 · 데이터베이스 만들기 (무료)

[Neon](https://neon.tech) 에 가입해 프로젝트를 만들고 연결 문자열을 복사합니다.

```
postgresql://사용자:비밀번호@ep-xxxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
```

- 무료 용량 0.5GB — 이 앱 기준 계정 수천 개, 연습 기록 수만 건 규모
- 쓰지 않을 때 자동으로 잠들고 접속하면 바로 깨어납니다
- Supabase·Railway 등 다른 PostgreSQL도 동일하게 쓸 수 있습니다
- **가입할 때 무료 요금제의 용량·보관 조건을 한 번 확인해 주세요** (업체 정책은 바뀝니다)

## 2단계 · Render 웹 서비스 만들기

1. [Render](https://render.com) → **New +** → **Web Service**
2. GitHub 저장소 `jezzyme/EGG` 연결 (`render.yaml`이 있어 Blueprint로도 가능)
3. 무료 플랜(Free) 선택
4. 아래 환경변수를 넣습니다

| 변수 | 값 | 비고 |
|---|---|---|
| `EGG_ENV` | `production` | HTTPS 강제·보안 쿠키·HSTS가 함께 켜집니다 |
| `EGG_SECRET_KEY` | (자동 생성) | 로그인 세션 서명 키. Render의 **Generate** 사용 |
| `EGG_DATABASE_URL` | 1단계에서 복사한 문자열 | **이 값이 없으면 데이터가 남지 않습니다** |
| `EGG_BASE_URL` | `https://<서비스명>.onrender.com` | 메일·구글 링크를 이 주소로 만듭니다 |
| `EGG_ALLOWED_HOSTS` | `<서비스명>.onrender.com` | 다른 주소로 온 요청을 거절합니다 |

빌드·실행 명령은 `render.yaml`에 들어 있습니다.

**Build Command**

```
pip install -r requirements.txt
```

**Start Command**

```
gunicorn app_flask:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120 --max-requests 500 --max-requests-jitter 50
```

| 옵션 | 뜻 |
|---|---|
| `app_flask:app` | `app_flask.py` 안의 `app` 객체를 실행 |
| `--bind 0.0.0.0:$PORT` | Render가 정해 주는 포트에 연결 (**반드시 필요**) |
| `--workers 2 --threads 4` | 동시에 8개 요청 처리. 메모리 부족 로그가 보이면 `--workers 1` 로 |
| `--timeout 120` | 자기소개서 파일 분석에 시간이 걸릴 수 있어 넉넉히 |
| `--max-requests 500` | 워커를 주기적으로 새로 띄워 메모리가 쌓이는 것을 방지 |

## 3단계 · 배포 확인

1. `https://<서비스명>.onrender.com/healthz` 접속

   ```json
   {"ok": true, "backend": "postgres", "location": "PostgreSQL (외부 데이터베이스)"}
   ```

   `"backend": "sqlite"` 로 나오면 `EGG_DATABASE_URL`이 전달되지 않은 것입니다 — **이 상태로 쓰면 데이터가 사라집니다.**
   `"ok": false` 면 `error` 항목에 원인이 적혀 있습니다.

2. 회원가입 → 면접 1회 → 재배포(Manual Deploy) → 다시 로그인
   **로그인이 유지되고 기록이 남아 있으면 정상입니다.**

## 무료 플랜에서 알아 둘 점

- **15분 동안 접속이 없으면 잠듭니다.** 다음 첫 접속이 30초~1분 걸립니다(데이터는 그대로).
- 메모리 512MB — 그래서 `faster-whisper`(서버 음성 인식)는 설치하지 않습니다.
  녹음 답변은 브라우저 음성 인식(Chrome·Edge)으로 처리되므로 기능에는 문제가 없습니다.
- 업로드 상한 10MB는 그대로 동작합니다(Vercel은 4.5MB 제한이 있어 이 기능과 충돌합니다).

## 선택 설정

### 구글 간편 로그인

| 변수 | 값 |
|---|---|
| `GOOGLE_CLIENT_ID` | 구글 클라우드 콘솔에서 발급 |
| `GOOGLE_CLIENT_SECRET` | 같은 화면에서 발급 |

구글 콘솔의 **승인된 리디렉션 URI**에 `https://<서비스명>.onrender.com/auth/google/callback` 을 추가해야 합니다.
설정하지 않으면 버튼이 자동으로 숨겨지고 이메일 로그인만 동작합니다.

### 비밀번호 재설정 메일

| 변수 | 예시 |
|---|---|
| `EGG_SMTP_HOST` | `smtp.gmail.com` |
| `EGG_SMTP_PORT` | `587` (465면 SSL) |
| `EGG_SMTP_USER` | `myaccount@gmail.com` |
| `EGG_SMTP_PASSWORD` | Gmail은 **앱 비밀번호** |
| `EGG_MAIL_FROM` | `no-reply@내도메인` |

설정하지 않으면 재설정 링크가 서버 로그에만 남습니다(화면 안내는 동일해 가입 여부가 드러나지 않습니다).

---

## 나중에 유료로 올릴 때

| 원하는 것 | 방법 |
|---|---|
| 잠들지 않게 하기 | Render 유료 플랜(Starter)으로 변경. 환경변수 그대로 |
| 데이터 용량 늘리기 | Neon 유료 플랜으로 변경. 연결 문자열 그대로 |
| SQLite로 돌아가기 | Render 디스크 추가 후 `EGG_DATABASE_URL` 삭제, `EGG_DB_PATH`를 디스크 경로로 지정 |

코드 수정은 필요 없습니다.

---

## 로컬 실행

```bash
pip install -r requirements.txt
python run_local.py          # http://127.0.0.1:5000
```

`.env.local.example`을 `.env.local`로 복사해 값을 채우면 구글 로그인·메일도 로컬에서 시험할 수 있습니다.

## 보관되는 정보와 보호 방식

- 계정(이름·이메일·비밀번호 해시), 연습 기록(프로필·답변·평가 결과)
- 답변과 자기소개서는 브라우저 쿠키가 아닌 서버에만 저장
- 업로드한 파일은 글자만 뽑은 뒤 즉시 폐기(저장하지 않음)
- 녹음 파일도 분석 후 삭제
- 연습 기록은 사용자당 최근 30회까지 보관, 사용자가 직접 삭제·탈퇴 가능
- SQLite 파일은 소유자 전용 권한(POSIX 600 / Windows icacls)으로 제한

## 요청 횟수 제한

| 대상 | 허용량 | 초과 시 |
|---|---|---|
| 로그인 실패 | 8회 / 5분 | 잠금 안내 |
| 회원가입 시도 | 12회 / 10분 (IP) | 안내 |
| 자기소개서 파일 분석 | 30회 / 10분 (사용자) | 429 + 안내 |
| 비밀번호 재설정 | 5회 / 1시간 | 안내는 동일, 실제 발송만 중단 |

## 환경변수 전체 목록

| 변수 | 필수 | 설명 |
|---|---|---|
| `EGG_SECRET_KEY` | ✅ | 세션 서명 키. 없으면 재시작 때 로그인이 풀립니다 |
| `EGG_DATABASE_URL` | ✅(배포) | PostgreSQL 연결 문자열. 없으면 SQLite 파일 사용 |
| `EGG_ENV` | 권장 | `production`이면 HTTPS 강제·보안 쿠키·HSTS |
| `EGG_BASE_URL` | 권장 | 외부 링크(메일·구글 콜백) 생성 기준 주소 |
| `EGG_ALLOWED_HOSTS` | 권장 | 허용할 접속 주소(쉼표 구분) |
| `EGG_DB_PATH` | 선택 | SQLite 파일 경로 |
| `EGG_DB_TIMEOUT` | 선택 | DB 연결 대기 초(기본 8) |
| `EGG_FORCE_HTTPS` | 선택 | HTTPS 강제를 개별 제어 |
| `EGG_COOKIE_SECURE` | 선택 | 보안 쿠키를 개별 제어 |
| `EGG_TRUST_PROXY_HOST` | 선택 | 프록시가 `X-Forwarded-Host`를 덮어쓸 때만 `1` |
| `EGG_DEBUG` | ❌ | 개발용. **운영에서 켜지 마세요**(원격 코드 실행 위험) |
