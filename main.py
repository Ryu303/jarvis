import os
import traceback
import base64
import sqlite3
from email.mime.text import MIMEText
import google.generativeai as genai
from pydantic import BaseModel
from datetime import datetime, time
from email.utils import parseaddr
from email.header import decode_header, make_header
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build


# .env 파일 로드
load_dotenv()

# 로컬 DB 초기화 (장기 기억 모듈)
def init_db():
    conn = sqlite3.connect("jarvis_memory.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# 로컬 개발 환경(HTTP)에서 OAuth 2.0 테스트가 가능하도록 설정
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

import secrets

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
# 보안 강화: 하드코딩 비밀키 대신 매 실행시 무작위 안전 키 자동 생성
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY")
if not SESSION_SECRET_KEY:
    SESSION_SECRET_KEY = secrets.token_hex(32)
TOKEN_FILE = "token.json"

# Gemini API 설정
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    print(f"Gemini API configured with key prefix: {GEMINI_API_KEY[:10]}...")
else:
    print("WARNING: GEMINI_API_KEY가 설정되지 않았습니다.")


if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
    raise ValueError("GOOGLE_CLIENT_ID와 GOOGLE_CLIENT_SECRET이 .env 파일에 올바르게 설정되지 않았습니다.")

app = FastAPI(title="Jarvis Backend API")

# OAuth 2.0 PKCE(code_verifier) 및 state 검증을 위해 세션 미들웨어 추가
# 보안 강화: same_site="lax" 세션 탈취(CSRF) 방지 설정 추가
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY, same_site="lax")

# 보안 강화: HTTP 응답 보안 헤더 미들웨어 정의
@app.middleware("http")
async def secure_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"  # 클릭재킹 공격 방지
    response.headers["X-Content-Type-Options"] = "nosniff"  # MIME 스니핑 방지
    response.headers["X-XSS-Protection"] = "1; mode=block"  # 브라우저 내장 XSS 필터링 활성화
    # Content Security Policy (기본 로컬 및 Google 폰트/스크립트 리소스만 허용, base64 오디오 재생 위해 media-src 허용)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: *; "
        "media-src 'self' data:;"
    )
    return response

# 접근 권한 (Scopes) 정의
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/spreadsheets"
]

REDIRECT_URI = "http://localhost:8000/callback"

def get_client_config():
    """.env 파일에서 읽어온 값으로 Google OAuth client_config 생성"""
    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": [REDIRECT_URI]
        }
    }

def get_credentials():
    """로컬 token.json에서 자격 증명을 로드하고, 만료된 경우 자동으로 갱신"""
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            # 토큰이 만료되었고 갱신 토큰(refresh_token)이 있는 경우 자동 갱신
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(GoogleRequest())
                with open(TOKEN_FILE, "w") as token_file:
                    token_file.write(creds.to_json())
                try:
                    os.chmod(TOKEN_FILE, 0o600)  # 보안 권한 설정 (소유자 전용 읽기/쓰기)
                except Exception:
                    pass
            return creds
        except Exception as e:
            print(f"토큰 로드 중 오류 발생: {e}")
            return None
    return None

# Jinja2 템플릿 설정
templates = Jinja2Templates(directory=".")

@app.api_route("/", methods=["GET", "HEAD"])
def read_root(request: Request):
    """자비스 대시보드 메인 페이지"""
    if request.method == "HEAD":
        return HTMLResponse(status_code=200)
    is_logged_in = os.path.exists(TOKEN_FILE)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"is_logged_in": is_logged_in}
    )


@app.get("/login")
def login(request: Request):
    """구글 로그인 동의 화면으로 리다이렉트"""
    flow = Flow.from_client_config(
        get_client_config(),
        scopes=SCOPES
    )
    flow.redirect_uri = REDIRECT_URI
    
    # 동의 화면 URL 생성 (오프라인 액세스, 동의 요구 및 계정 선택 활성화)
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent select_account'
    )
    
    # 세션에 state와 PKCE 인증을 위한 code_verifier 저장
    request.session["state"] = state
    request.session["code_verifier"] = flow.code_verifier
    
    return RedirectResponse(url=authorization_url)

@app.get("/callback")
def callback(request: Request, code: str = None, state: str = None, error: str = None):
    """구글 로그인 후 인증 코드를 받아 토큰으로 교환 및 로컬 저장"""
    if error:
        raise HTTPException(status_code=400, detail=f"Google Authentication Error: {error}")
    
    # 세션에서 기존에 저장해둔 state와 code_verifier 조회
    stored_state = request.session.get("state")
    code_verifier = request.session.get("code_verifier")
    
    # CSRF 보호를 위한 state 검증
    if not state or state != stored_state:
        raise HTTPException(status_code=400, detail="인증 상태(state)가 일치하지 않습니다. CSRF 공격이 의심됩니다.")
    
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code.")
    
    try:
        # Flow 객체를 다시 빌드하여 토큰 요청 진행
        flow = Flow.from_client_config(
            get_client_config(),
            scopes=SCOPES
        )
        flow.redirect_uri = REDIRECT_URI
        flow.code_verifier = code_verifier
        
        # 인증 코드를 사용해 토큰 획득
        flow.fetch_token(code=code)
        credentials = flow.credentials
        
        # 얻어낸 credentials(액세스 토큰, 리프레시 토큰 등)를 로컬 token.json에 저장
        with open(TOKEN_FILE, "w") as token_file:
            token_file.write(credentials.to_json())
        try:
            os.chmod(TOKEN_FILE, 0o600)  # 보안 권한 설정 (소유자 전용 읽기/쓰기)
        except Exception:
            pass
        
        # google-api-python-client를 사용하여 사용자 정보 가져오기
        service = build("oauth2", "v2", credentials=credentials)
        user_info = service.userinfo().get().execute()
        
        email = user_info.get("email")
        
        return {
            "message": "자비스 구글 연동 성공! 자격 증명이 token.json에 저장되었습니다.",
            "email": email,
            "user_info": {
                "name": user_info.get("name"),
                "picture": user_info.get("picture")
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Authentication Process Failed: {str(e)}")

@app.get("/calendar/today")
def get_today_calendar():
    """로그인한 사용자의 오늘 일정 리스트 조회"""
    creds = get_credentials()
    if not creds:
        raise HTTPException(
            status_code=401, 
            detail="로그인이 필요합니다. http://localhost:8000/login 에서 먼저 인증해 주세요."
        )
    
    try:
        # 구글 캘린더 서비스 빌드
        service = build("calendar", "v3", credentials=creds)
        
        # 오늘의 로컬 자정부터 자정까지의 시간 계산 (로컬 타임존 적용)
        now = datetime.now()
        local_tz = datetime.now().astimezone().tzinfo
        
        start_of_today = datetime.combine(now.date(), time.min).replace(tzinfo=local_tz).isoformat()
        end_of_today = datetime.combine(now.date(), time.max).replace(tzinfo=local_tz).isoformat()
        
        # primary 캘린더에서 오늘의 이벤트 리스트 요청
        events_result = service.events().list(
            calendarId='primary',
            timeMin=start_of_today,
            timeMax=end_of_today,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        
        # 일정 데이터 포맷 정제
        formatted_events = []
        for event in events:
            title = event.get('summary', '(제목 없음)')
            start = event.get('start', {}).get('dateTime') or event.get('start', {}).get('date')
            end = event.get('end', {}).get('dateTime') or event.get('end', {}).get('date')
            formatted_events.append({
                "title": title,
                "start": start,
                "end": end
            })
            
        if not formatted_events:
            return {"message": "오늘 예정된 일정이 없습니다."}
            
        return formatted_events
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"구글 캘린더 API 호출 중 오류 발생: {str(e)}")


def clean_sender_name(sender_raw):
    if not sender_raw:
        return "(보낸 사람 없음)"
    try:
        # MIME 인코딩 헤더 디코딩 (예: =?utf-8?B?...?=)
        decoded = str(make_header(decode_header(sender_raw)))
    except Exception:
        decoded = sender_raw
    
    # 이메일 주소에서 이름 파싱
    name, addr = parseaddr(decoded)
    if name:
        return name
    return addr if addr else decoded


@app.get("/gmail/unread")
def get_unread_emails():
    """로그인한 사용자의 읽지 않은 최신 메일 최대 5개 조회"""
    try:
        creds = get_credentials()
        if not creds:
            raise HTTPException(
                status_code=401, 
                detail="로그인이 필요합니다. http://localhost:8000/login 에서 먼저 인증해 주세요."
            )
        
        # 구글 Gmail 서비스 빌드
        service = build("gmail", "v1", credentials=creds)
        
        # 읽지 않은 받은 편지함 메일 목록 요청 (최대 5개)
        results = service.users().messages().list(
            userId='me',
            q='is:unread label:INBOX',
            maxResults=5
        ).execute()
        
        messages = results.get('messages', [])
        
        if not messages:
            return {"message": "읽지 않은 최근 메일이 없습니다."}
            
        formatted_messages = []
        for message in messages:
            msg_id = message.get('id')
            if not msg_id:
                continue
                
            # 메타데이터 포맷으로 가볍게 조회
            msg_detail = service.users().messages().get(
                userId='me', 
                id=msg_id, 
                format='metadata', 
                metadataHeaders=['From', 'Subject']
            ).execute()
            
            if not msg_detail:
                continue
                
            snippet = msg_detail.get('snippet', '')
            payload = msg_detail.get('payload') or {}
            headers = payload.get('headers') or []
            
            subject = "(제목 없음)"
            sender_raw = "(보낸 사람 없음)"
            
            for header in headers:
                name = header.get('name', '')
                value = header.get('value', '')
                if name.lower() == 'subject':
                    subject = value
                elif name.lower() == 'from':
                    sender_raw = value
            
            # 보낸 사람 이름과 제목 정제
            sender = clean_sender_name(sender_raw)
            try:
                subject_decoded = str(make_header(decode_header(subject)))
            except Exception:
                subject_decoded = subject
            
            formatted_messages.append({
                "from": sender,
                "subject": subject_decoded,
                "snippet": snippet
            })
            
        return formatted_messages
        
    except HTTPException as he:
        # FastAPI HTTP 예외 발생 시 상세 로그 기록 후 에러 반환
        print("HTTPException in get_unread_emails:")
        traceback.print_exc()
        return {"error": he.detail}
    except Exception as e:
        # 일반 예외 발생 시 서버 터미널에 상세 에러 출력
        print("Unhandled Exception in get_unread_emails:")
        traceback.print_exc()
        
        error_msg = str(e)
        # 메일 서비스가 활성화되지 않은 계정(예: 네이버 등 외부 메일 주소로 가입한 구글 계정)인 경우
        if "Mail service not enabled" in error_msg:
            return {
                "message": "Gmail 서비스가 활성화되지 않은 계정입니다.",
                "details": "구글 계정이 @gmail.com 주소가 아니거나 Gmail 사서함이 활성화되어 있지 않습니다. 실제 지메일 계정으로 다시 연동해 주세요."
            }
            
        return {"error": f"구글 Gmail API 호출 및 파싱 중 오류 발생: {error_msg}"}


def check_today_schedule() -> str:
    """오늘의 구글 캘린더 일정을 조회합니다. 사용자 일정, 스케줄을 확인하거나 물어볼 때 사용합니다."""
    creds = get_credentials()
    if not creds:
        return "로그인이 되어 있지 않습니다. 대시보드 로그인 버튼을 통해 먼저 구글 연동 로그인을 완료해 주세요."
    try:
        service = build("calendar", "v3", credentials=creds)
        now = datetime.now()
        local_tz = datetime.now().astimezone().tzinfo
        start_of_today = datetime.combine(now.date(), time.min).replace(tzinfo=local_tz).isoformat()
        end_of_today = datetime.combine(now.date(), time.max).replace(tzinfo=local_tz).isoformat()
        
        events_result = service.events().list(
            calendarId='primary',
            timeMin=start_of_today,
            timeMax=end_of_today,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        if not events:
            return "오늘 예정된 일정이 없습니다."
            
        formatted_events = []
        for event in events:
            title = event.get('summary', '(제목 없음)')
            start = event.get('start', {}).get('dateTime') or event.get('start', {}).get('date')
            end = event.get('end', {}).get('dateTime') or event.get('end', {}).get('date')
            event_id = event.get('id', '')
            formatted_events.append(f"- {title} (시작: {start}, 종료: {end}, ID: {event_id})")
        return "\n".join(formatted_events)
    except Exception as e:
        return f"캘린더 일정을 불러오는 도중 오류가 발생했습니다: {str(e)}"


def check_unread_emails() -> str:
    """최근 읽지 않은 이메일 목록을 가져옵니다. 반환된 데이터에는 '보낸 사람', '제목', 그리고 메일 내용의 일부인 '미리보기 텍스트(Snippet)'가 포함되어 있습니다. 사용자가 메일 요약을 요청하면 반드시 이 함수를 호출하여 데이터를 확인한 뒤, Snippet 내용을 바탕으로 핵심만 요약해서 대답하세요."""
    creds = get_credentials()
    if not creds:
        return "로그인이 되어 있지 않습니다. 대시보드 로그인 버튼을 통해 먼저 구글 연동 로그인을 완료해 주세요."
    try:
        service = build("gmail", "v1", credentials=creds)
        results = service.users().messages().list(
            userId='me',
            q='is:unread label:INBOX',
            maxResults=5
        ).execute()
        
        messages = results.get('messages', [])
        if not messages:
            return "읽지 않은 최근 메일이 없습니다."
            
        formatted_messages = []
        for message in messages:
            msg_id = message.get('id')
            if not msg_id:
                continue
            msg_detail = service.users().messages().get(
                userId='me', 
                id=msg_id, 
                format='metadata', 
                metadataHeaders=['From', 'Subject']
            ).execute()
            if not msg_detail:
                continue
            snippet = msg_detail.get('snippet', '')
            payload = msg_detail.get('payload') or {}
            headers = payload.get('headers') or []
            
            subject = "(제목 없음)"
            sender_raw = "(보낸 사람 없음)"
            for header in headers:
                name = header.get('name', '')
                value = header.get('value', '')
                if name.lower() == 'subject':
                    subject = value
                elif name.lower() == 'from':
                    sender_raw = value
            
            sender = clean_sender_name(sender_raw)
            try:
                subject_decoded = str(make_header(decode_header(subject)))
            except Exception:
                subject_decoded = subject
                
            formatted_messages.append(f"- 보낸사람: {sender}\n  제목: {subject_decoded}\n  미리보기 텍스트(Snippet): {snippet}")
        return "\n\n".join(formatted_messages)
    except Exception as e:
        error_msg = str(e)
        if "Mail service not enabled" in error_msg:
            return "지메일 서비스가 활성화되지 않은 계정입니다. @gmail.com 주소의 구글 계정으로 다시 연동해 주세요."
        return f"이메일을 불러오는 도중 오류가 발생했습니다: {error_msg}"


def Calendar(title: str, start_time: str, end_time: str) -> str:
    """사용자가 일정 추가, 등록, 생성을 지시하면 이 함수를 호출하여 구글 캘린더에 일정을 생성합니다.
    인자:
    - title: 일정 제목 (예: '치과 예약', '팀 회의')
    - start_time: 일정 시작 시간 (ISO 8601 포맷 문자열, 예: '2026-06-16T14:00:00+09:00' 또는 '2026-06-16T14:00:00')
    - end_time: 일정 종료 시간 (ISO 8601 포맷 문자열, 예: '2026-06-16T15:00:00+09:00' 또는 '2026-06-16T15:00:00')
    """
    def format_iso_time(t_str: str) -> str:
        # 1. replace space with 'T'
        t_str = t_str.strip().replace(" ", "T")
        
        # 2. Check if timezone is specified.
        has_tz = False
        if 'T' in t_str:
            date_part, time_part = t_str.split('T', 1)
            if '+' in time_part or '-' in time_part or time_part.endswith('Z'):
                has_tz = True
        else:
            if len(t_str) == 10:
                t_str += "T00:00:00"

        if not has_tz:
            if 'T' in t_str:
                date_part, time_part = t_str.split('T', 1)
                colons = time_part.count(':')
                if colons == 1:
                    time_part += ":00"
                elif colons == 0:
                    time_part += ":00:00"
                t_str = f"{date_part}T{time_part}+09:00"
            else:
                t_str += "T00:00:00+09:00"
        else:
            tz_char = ""
            if 'Z' in t_str:
                tz_char = 'Z'
            elif '+' in t_str:
                tz_char = '+'
            elif '-' in t_str:
                if 'T' in t_str:
                    _, time_part = t_str.split('T', 1)
                    if '-' in time_part:
                        tz_char = '-'
            
            if tz_char:
                if tz_char == 'Z':
                    main_part, tz_part = t_str.rsplit('Z', 1)
                    tz_suffix = 'Z'
                else:
                    main_part, tz_part = t_str.rsplit(tz_char, 1)
                    tz_suffix = tz_char + tz_part
                
                if 'T' in main_part:
                    date_part, time_part = main_part.split('T', 1)
                    colons = time_part.count(':')
                    if colons == 1:
                        time_part += ":00"
                    elif colons == 0:
                        time_part += ":00:00"
                    t_str = f"{date_part}T{time_part}{tz_suffix}"

        return t_str

    creds = get_credentials()
    if not creds:
        return "로그인이 되어 있지 않습니다. 대시보드 로그인 버튼을 통해 먼저 구글 연동 로그인을 완료해 주세요."
    try:
        formatted_start = format_iso_time(start_time)
        formatted_end = format_iso_time(end_time)
        service = build("calendar", "v3", credentials=creds)
        event = {
            'summary': title,
            'start': {
                'dateTime': formatted_start,
                'timeZone': 'Asia/Seoul',
            },
            'end': {
                'dateTime': formatted_end,
                'timeZone': 'Asia/Seoul',
            },
        }
        created_event = service.events().insert(calendarId='primary', body=event).execute()
        return f"일정이 구글 캘린더에 성공적으로 생성되었습니다. (제목: {title}, 시작: {formatted_start}, 종료: {formatted_end}, 일정 링크: {created_event.get('htmlLink')})"
    except Exception as e:
        return f"구글 캘린더 일정 생성 중 오류가 발생했습니다: {str(e)}"


def send_email(to: str, subject: str, body: str) -> str:
    """사용자가 이메일(메일) 전송 또는 발송을 지시하면 이 함수를 호출하여 이메일을 전송합니다.
    인자:
    - to: 수신자의 이메일 주소 (예: 'example@domain.com')
    - subject: 이메일 제목 (예: '보고서 전달')
    - body: 이메일 본문 내용 (예: '안녕하세요, 요청하신 자료를 보냅니다.')
    """
    creds = get_credentials()
    if not creds:
        return "로그인이 되어 있지 않습니다. 대시보드 로그인 버튼을 통해 먼저 구글 연동 로그인을 완료해 주세요."
    try:
        service = build("gmail", "v1", credentials=creds)
        message = MIMEText(body)
        message['to'] = to
        message['subject'] = subject
        
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        body_payload = {'raw': raw_message}
        
        sent_message = service.users().messages().send(userId='me', body=body_payload).execute()
        return f"이메일이 성공적으로 발송되었습니다. (수신자: {to}, 메일 ID: {sent_message.get('id')})"
    except Exception as e:
        return f"이메일 발송 중 오류가 발생했습니다: {str(e)}"


def search_drive_files(query: str) -> str:
    """구글 드라이브(Google Drive)에서 파일을 검색합니다. 사용자가 특정 문서를 찾거나 파일 검색을 지시했을 때 이 함수를 사용합니다.
    인자:
    - query: 검색할 파일의 이름이나 키워드 (예: '보고서', '회의록')
    """
    creds = get_credentials()
    if not creds:
        return "로그인이 되어 있지 않습니다. 대시보드 로그인 버튼을 통해 먼저 구글 연동 로그인을 완료해 주세요."
    try:
        service = build("drive", "v3", credentials=creds)
        q_str = f"name contains '{query}'"
        results = service.files().list(
            q=q_str,
            pageSize=3,
            fields="files(id, name, mimeType, webViewLink)"
        ).execute()
        files = results.get('files', [])
        if not files:
            return f"구글 드라이브에서 '{query}' 관련 파일을 찾지 못했습니다."
        
        formatted_files = []
        for f in files:
            name = f.get('name', '(이름 없음)')
            link = f.get('webViewLink', '(링크 없음)')
            mime_type = f.get('mimeType', '(알 수 없음)')
            formatted_files.append(f"- 이름: {name}\n  타입: {mime_type}\n  링크: {link}")
        return "\n".join(formatted_files)
    except Exception as e:
        return f"구글 드라이브 검색 중 오류가 발생했습니다: {str(e)}"


def manage_tasks(action: str, title: str = None) -> str:
    """구글 할 일(Google Tasks) 목록을 조회하거나 새 할 일을 추가합니다. 사용자가 할 일 리스트 확인을 원하거나 할 일(To-Do) 등록을 지시하면 사용합니다.
    인자:
    - action: 수행할 행동 ('list' 또는 'insert')
    - title: 추가할 할 일의 제목 (action이 'insert'일 때 필수 입력, 예: '장보기', '회의 준비')
    """
    creds = get_credentials()
    if not creds:
        return "로그인이 되어 있지 않습니다. 대시보드 로그인 버튼을 통해 먼저 구글 연동 로그인을 완료해 주세요."
    try:
        service = build("tasks", "v1", credentials=creds)
        if action == 'list':
            results = service.tasks().list(tasklist='@default').execute()
            tasks = results.get('items', [])
            if not tasks:
                return "현재 등록된 할 일이 없습니다."
            formatted_tasks = []
            for t in tasks:
                t_title = t.get('title', '(제목 없음)')
                status = t.get('status', 'needsAction')
                status_str = "완료" if status == "completed" else "진행 중"
                formatted_tasks.append(f"- {t_title} [{status_str}]")
            return "\n".join(formatted_tasks)
        elif action == 'insert':
            if not title:
                return "할 일을 추가하기 위해서는 제목(title)을 입력하셔야 합니다."
            task_body = {'title': title}
            created_task = service.tasks().insert(tasklist='@default', body=task_body).execute()
            return f"구글 할 일 목록에 성공적으로 등록되었습니다. (제목: {title}, ID: {created_task.get('id')})"
        else:
            return "올바르지 않은 action입니다. 'list' 또는 'insert'를 지정해 주세요."
    except Exception as e:
        return f"구글 할 일 관리 중 오류가 발생했습니다: {str(e)}"


def append_sheet_data(spreadsheet_id: str, range_name: str, values: str) -> dict:
    """구글 스프레드시트(Google Sheets)에 데이터를 추가(append)합니다. 사용자가 엑셀이나 시트에 데이터를 기록, 저장, 추가하도록 요청할 때 사용합니다.
    인자:
    - spreadsheet_id: 데이터를 추가할 스프레드시트의 ID 고유값 (스프레드시트 URL에서 추출된 문자열)
    - range_name: 데이터를 쓸 시트 이름 및 범위 (예: 'Sheet1!A:C' 또는 '시트1!A1')
    - values: 추가할 행 데이터들을 담은 JSON 형식의 2차원 리스트 문자열 (예: '[["2026-06-16", "회의 완료", "비고"]]')
    """
    print(f"[append_sheet_data] Called with ID: {spreadsheet_id}, Range: {range_name}, Values: {values}")
    try:
        import json
        # JSON 문자열 형식인 경우 안전하게 파싱 시도
        if isinstance(values, str):
            try:
                values = json.loads(values)
            except Exception:
                # 파싱 실패 시 일반 문자열 데이터로 취급하여 감쌈
                values = [[values]]

        # protobuf 객체 등을 순수 파이썬 자료형(list, dict 등)으로 강제 변환
        def to_native(obj):
            if isinstance(obj, dict):
                return {k: to_native(v) for k, v in obj.items()}
            elif isinstance(obj, (list, tuple)) or (hasattr(obj, '__iter__') and not isinstance(obj, (str, bytes))):
                return [to_native(item) for item in obj]
            return obj

        values = to_native(values)
        
        # 1차원 리스트 형태를 2차원 리스트로 무조건 변환
        if not isinstance(values, list):
            values = [[values]]
        elif len(values) > 0 and not isinstance(values[0], list):
            values = [values]
            
        if not values or not values[0]:
            return {"status": "error", "message": "데이터(values) 형식이 잘못되었거나 비어있습니다. [ [열1, ...], [열1, ...] ] 형태여야 합니다."}

        # 입력 범위 강제 우회: 어떤 입력이든 '시트이름!A:B' 형태로 강제 치환
        if range_name:
            if '!' in range_name:
                sheet_name = range_name.split('!', 1)[0]
                range_name = f"{sheet_name}!A:B"
            else:
                import re
                clean_range = range_name.strip()
                # A1, B33 같은 형태이거나 A:B 처럼 좌표 형태인 경우
                if (re.match(r"^[A-Za-z]+\d*$", clean_range) and not re.match(r"^[A-Za-z]+$", clean_range)) or re.match(r"^[A-Za-z]+:[A-Za-z]+$", clean_range):
                    range_name = "A:B"
                else:
                    # 일반 단어(시트명)인 경우
                    range_name = f"{clean_range}!A:B"
            print(f"[append_sheet_data] range_name forced bypass to: {range_name}")

        creds = get_credentials()
        if not creds:
            return {"status": "error", "message": "로그인이 되어 있지 않습니다. 대시보드 로그인 버튼을 통해 먼저 구글 연동 로그인을 완료해 주세요."}
            
        service = build("sheets", "v4", credentials=creds)
        body = {
            'values': values
        }
        result = service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body=body
        ).execute()
        
        # API 결과 객체에서 updates 정보 확인 후 심플한 응답 반환
        ret = {
            "status": "success",
            "message": "성공적으로 추가되었습니다."
        }
        print(f"[append_sheet_data] API Success: {ret}")
        return ret
    except Exception as e:
        ret = {"status": "error", "message": f"상세 에러 내용: {str(e)}"}
        print(f"[append_sheet_data] Exception occurred: {str(e)}")
        return ret


def delete_calendar_event(event_id: str) -> str:
    """구글 캘린더(Google Calendar)에서 특정 일정을 삭제합니다. 사용자가 일정을 삭제하도록 요청할 때 사용합니다.
    인자:
    - event_id: 삭제할 일정의 고유 ID (예: 'abc123xyz')
    """
    creds = get_credentials()
    if not creds:
        return "로그인이 되어 있지 않습니다. 대시보드 로그인 버튼을 통해 먼저 구글 연동 로그인을 완료해 주세요."
    try:
        service = build("calendar", "v3", credentials=creds)
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        return f"일정(ID: {event_id})을 성공적으로 삭제했습니다."
    except Exception as e:
        return f"일정 삭제 중 오류가 발생했습니다: {str(e)}"


def clear_chat_history() -> str:
    """사용자가 대화 기록을 초기화하거나 기억을 지워달라고 요청하면 이 함수를 호출하여 SQLite DB의 대화 기억을 완전히 삭제합니다."""
    try:
        conn = sqlite3.connect("jarvis_memory.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM messages")
        conn.commit()
        conn.close()
        return "대화 기록과 기억이 완전히 초기화되었습니다."
    except Exception as e:
        return f"기억 초기화 중 오류가 발생했습니다: {str(e)}"


def get_current_time() -> str:
    """현재 날짜와 시간을 조회합니다. 오늘이 몇 일인지, 지금이 몇 시인지 등 현재 시간/날짜 정보가 필요할 때 이 함수를 사용합니다."""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def synthesize_speech(text: str) -> str:
    """구글 클라우드 Text-to-Speech API를 사용하여 텍스트를 고품질 남성 음성 MP3로 변환하고 base64 문자열로 반환합니다."""
    import os
    api_key = os.getenv("GCP_API_KEY") or GEMINI_API_KEY
    if not api_key:
        return None
    try:
        import urllib.request
        import json
        
        # HTML 태그나 특수 마크다운 등 일부 기호 정제
        clean_text = text.replace("*", " ").replace("_", " ").replace("`", " ").replace("#", " ")
        
        url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={api_key}"
        headers = {
            "Content-Type": "application/json"
        }
        body = {
            "input": {"text": clean_text},
            "voice": {
                "languageCode": "ko-KR",
                "name": "ko-KR-Neural2-C",  # 최신 프리미엄 남성 목소리
                "ssmlGender": "MALE"
            },
            "audioConfig": {
                "audioEncoding": "MP3",
                "speakingRate": 1.5,  # 1.5배속
                "pitch": -0.5         # 자비스 중저음 톤 (발음 또렷하게 조절)
            }
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req) as response:
            res_body = json.loads(response.read().decode("utf-8"))
            return res_body.get("audioContent")
    except Exception as e:
        print("Google Cloud TTS Error:", e)
        return None


# Gemini Generative Model 설정 (모든 조회, 행동 및 감각 도구들 등록 및 시스템 지침 강화)
if GEMINI_API_KEY:
    gemini_model = genai.GenerativeModel(
        model_name="models/gemini-2.5-flash",
        tools=[check_today_schedule, check_unread_emails, Calendar, send_email, search_drive_files, manage_tasks, append_sheet_data, delete_calendar_event, clear_chat_history, get_current_time],
        system_instruction="너는 사용자의 개인 비서 자비스(JARVIS)야. 너는 사용자에게 전달받은 함수(Tool)의 결과값을 단순 나열하지 않고, 지능적으로 분석하고 요약할 수 있는 능력이 있어. 메일 데이터를 받으면 본문 내용(Snippet)을 파악하여 중요도를 판단하고 친절하게 요약해 줘. 이제 너는 일정을 읽는 것뿐만 아니라 직접 캘린더에 일정을 생성하고, 직접 이메일을 발송할 수 있는 완벽한 비서야. 너는 이제 구글 드라이브 문서 검색, To-Do(할 일) 리스트 관리, 그리고 구글 스프레드시트에 데이터를 직접 기록할 수 있는 완벽한 전천후 비서야. 너는 이제 일정을 삭제하고 대화 기록을 초기화할 수 있는 관리 권한을 가졌어. 하지만 데이터 삭제는 위험한 작업이므로, 사용자가 삭제를 명확히 요청했을 때만 수행해. 삭제 도구를 호출하기 전, 만약 필요한 경우 사용자에게 한 번 더 확인할 수도 있어. 사용자가 일정을 등록, 추가, 생성해달라고 지시하면, 너의 임의 상상으로 등록이 완료되었다고 응답하지 말고, 반드시 Calendar 툴을 호출해 성공 결과(일정 링크 포함)를 직접 리턴받은 후 이를 바탕으로 사용자에게 답변하라. 또한, 사용자가 특정 일정이나 회의를 삭제해 달라고 요청하면, 먼저 check_today_schedule 등의 도구를 호출하여 삭제하고자 하는 일정의 고유 ID(event_id)를 찾은 뒤, delete_calendar_event 도구를 그 ID로 호출하여 삭제를 완료하라. 여러 일정을 지우고자 하거나 중복된 일정이 있다면, 각각의 ID를 확인하여 delete_calendar_event를 필요한 만큼 여러 번 호출하여 한 번에 지워라. 절대 사용자에게 고유 ID값을 직접 물어보지 말고, 도구 조회를 통해 스스로 알아내어 삭제하라. 사용자가 오늘, 내일, 특정 날짜/시간과 관련된 일정을 질문하거나 조작(생성, 삭제 등)을 요청할 때 정확한 기준 날짜/시간이 필요하다면, 반드시 get_current_time 도구를 먼저 호출하여 현재 실시간 시각을 확인한 뒤 연산하라. 또한, 너는 음성 비서이므로 사용자와 대화할 때 텍스트 답변이 구어체(말하는 말투)로 자연스럽고 매끄럽게 작성되도록 해줘. 글머리 기호(마크다운), 대괄호, 코드 블록, 특수 기호는 완전히 피하고, 실제 사람이 귀에 대고 말하듯이 부드럽고 격식 있는 문장으로 대답해라."
    )
else:
    gemini_model = None


def load_chat_history():
    """DB에서 최근 10개의 대화 내용을 조회하여 Gemini history 포맷으로 변환합니다."""
    try:
        conn = sqlite3.connect("jarvis_memory.db")
        cursor = conn.cursor()
        # 최근 10개의 대화를 가져와서 시간순(ID 오름차순)으로 반환
        cursor.execute("""
            SELECT role, content FROM (
                SELECT id, role, content FROM messages ORDER BY id DESC LIMIT 10
            ) ORDER BY id ASC
        """)
        rows = cursor.fetchall()
        conn.close()
        
        history = []
        for role, content in rows:
            history.append({
                "role": role,
                "parts": [content]
            })
        return history
    except Exception as e:
        print(f"Error loading chat history from DB: {e}")
        return []


def save_chat_message(role: str, content: str):
    """대화 메시지를 DB에 저장합니다."""
    try:
        conn = sqlite3.connect("jarvis_memory.db")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO messages (role, content) VALUES (?, ?)", (role, content))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error saving chat message to DB: {e}")


class ChatMessageRequest(BaseModel):
    message: str


@app.post("/ai/chat")
def ai_chat(payload: ChatMessageRequest):
    """제미나이 AI와 대화하는 채팅 엔드포인트 (기억 조회 및 저장 반영)"""
    if not gemini_model:
        return {
            "response": "서버의 .env 파일에 GEMINI_API_KEY가 설정되지 않았습니다. API 키를 먼저 등록해 주세요."
        }
        
    try:
        user_message = payload.message
        
        # 1. DB에서 과거 대화 기록(최대 10개) 조회 및 변환
        history = load_chat_history()
        
        # 2. 대화 기록을 포함하여 대화 세션 시작
        chat = gemini_model.start_chat(history=history, enable_automatic_function_calling=True)
        
        # 3. 메시지 전송 및 답변 획득
        response = chat.send_message(user_message)
        ai_response = response.text if response.text else "(자비스 코어로부터 텍스트 응답을 가져오지 못했습니다.)"
        
        # 4. 사용자의 질문과 AI의 답변을 DB에 각각 저장
        save_chat_message("user", user_message)
        save_chat_message("model", ai_response)
        
        # 5. 구글 클라우드 TTS를 사용해 고품질 음성 데이터 합성
        audio_content = synthesize_speech(ai_response)
        
        return {"response": ai_response, "audio": audio_content}
    except Exception as e:
        print("Gemini API Error:")
        traceback.print_exc()
        
        # API 소진 및 호출 한도 초과(429, ResourceExhausted) 예외 감지
        error_msg = str(e)
        if "429" in error_msg or "ResourceExhausted" in error_msg or "quota" in error_msg.lower() or "exhausted" in error_msg.lower():
            return {
                "response": "⚠️ [API 할당량 초과 경고] 현재 제미나이(Gemini) API 호출 한도가 모두 소진되었습니다. 무료 요금제 제공 한도(예: 분당 최대 15회 요청)를 초과했거나 일일 할당량이 부족할 수 있으니, 잠시 후에 다시 시도해 주시기 바랍니다."
            }
            
        return {"response": f"자비스 AI 코어 응답 생성 중 오류 발생: {str(e)}"}


@app.delete("/ai/chat/history")
def clear_chat_history_endpoint():
    """DB에 저장된 대화 기억(메시지 기록)을 모두 삭제합니다."""
    res = clear_chat_history()
    if "오류" in res:
        raise HTTPException(status_code=500, detail=res)
    return {"message": "대화 기억이 성공적으로 초기화되었습니다."}



