import os
import traceback
import base64
import sqlite3
import json
import numpy as np
from email.mime.text import MIMEText
import google.generativeai as genai
from pydantic import BaseModel
from datetime import datetime, time
from email.utils import parseaddr
from email.header import decode_header, make_header
from fastapi import FastAPI, Request, HTTPException, File, UploadFile, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build


# .env 파일 로드
load_dotenv()

# 로컬 DB 초기화 (장기 기억 모듈 및 사용자 프로필 장기 기억 테이블)
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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            message TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            read INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS voice_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            features TEXT,
            label TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        cursor.execute("ALTER TABLE voice_profiles ADD COLUMN label TEXT")
    except Exception:
        pass
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS voice_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS consultations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_name TEXT,
            structured_note TEXT,
            docs_url TEXT,
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

# ElevenLabs API 설정
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")

if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
    raise ValueError("GOOGLE_CLIENT_ID와 GOOGLE_CLIENT_SECRET이 .env 파일에 올바르게 설정되지 않았습니다.")

app = FastAPI(title="Jarvis Backend API")

# static 폴더 마운트 (효과음 재생용)
app.mount("/static", StaticFiles(directory="static"), name="static")

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
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents"
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
                "id": msg_id,
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


def check_schedule_in_range(start_date: str = None, end_date: str = None, **kwargs) -> str:
    """구글 캘린더 일정을 지정된 날짜/시간 범위 내에서 조회합니다.

    Args:
        start_date: 조회 시작 날짜/시간 (형식: 'YYYY-MM-DD' 또는 'YYYY-MM-DDTHH:MM:SS+09:00', 생략 가능)
        end_date: 조회 종료 날짜/시간 (형식: 'YYYY-MM-DD' 또는 'YYYY-MM-DDTHH:MM:SS+09:00', 생략 가능)
    """
    creds = get_credentials()
    if not creds:
        return "로그인이 되어 있지 않습니다. 대시보드 로그인 버튼을 통해 먼저 구글 연동 로그인을 완료해 주세요."
    try:
        service = build("calendar", "v3", credentials=creds)
        now = datetime.now()
        local_tz = datetime.now().astimezone().tzinfo
        
        # 시작 시간 설정
        if not start_date:
            start_dt = datetime.combine(now.date(), time.min).replace(tzinfo=local_tz)
        else:
            start_date = start_date.strip()
            if len(start_date) == 10:
                dt = datetime.strptime(start_date, "%Y-%m-%d")
                start_dt = datetime.combine(dt.date(), time.min).replace(tzinfo=local_tz)
            else:
                try:
                    start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=local_tz)
                except ValueError:
                    start_dt = datetime.combine(now.date(), time.min).replace(tzinfo=local_tz)

        # 종료 시간 설정
        if not end_date:
            if start_date:
                end_dt = datetime.combine(start_dt.date(), time.max).replace(tzinfo=local_tz)
            else:
                end_dt = datetime.combine(now.date(), time.max).replace(tzinfo=local_tz)
        else:
            end_date = end_date.strip()
            if len(end_date) == 10:
                dt = datetime.strptime(end_date, "%Y-%m-%d")
                end_dt = datetime.combine(dt.date(), time.max).replace(tzinfo=local_tz)
            else:
                try:
                    end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=local_tz)
                except ValueError:
                    end_dt = datetime.combine(start_dt.date(), time.max).replace(tzinfo=local_tz)

        timeMin_iso = start_dt.isoformat()
        timeMax_iso = end_dt.isoformat()
        
        events_result = service.events().list(
            calendarId='primary',
            timeMin=timeMin_iso,
            timeMax=timeMax_iso,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        if not events:
            return f"{start_dt.strftime('%Y-%m-%d')}부터 {end_dt.strftime('%Y-%m-%d')} 사이에 예정된 일정이 없습니다."
            
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
                
            formatted_messages.append(f"- ID: {msg_id}\n  보낸사람: {sender}\n  제목: {subject_decoded}\n  미리보기 텍스트(Snippet): {snippet}")
        return "\n\n".join(formatted_messages)
    except Exception as e:
        error_msg = str(e)
        if "Mail service not enabled" in error_msg:
            return "지메일 서비스가 활성화되지 않은 계정입니다. @gmail.com 주소의 구글 계정으로 다시 연동해 주세요."
        return f"이메일을 불러오는 도중 오류가 발생했습니다: {error_msg}"


def mark_email_as_read(message_id: str, **kwargs) -> str:
    """구글 Gmail에서 특정 이메일(ID 기준)을 읽음 처리합니다. (UNREAD 라벨을 제거합니다.)
    
    Args:
        message_id: 읽음 처리할 이메일 메시지의 고유 ID
    """
    creds = get_credentials()
    if not creds:
        return "로그인이 되어 있지 않습니다. 대시보드 로그인 버튼을 통해 먼저 구글 연동 로그인을 완료해 주세요."
    try:
        service = build("gmail", "v1", credentials=creds)
        # UNREAD 라벨을 제거하여 읽음 처리
        service.users().messages().batchModify(
            userId='me',
            body={
                'ids': [message_id],
                'removeLabelIds': ['UNREAD']
            }
        ).execute()
        return f"성공적으로 이메일(ID: {message_id})을 읽음 처리했습니다."
    except Exception as e:
        error_msg = str(e)
        if "Mail service not enabled" in error_msg:
            return "지메일 서비스가 활성화되지 않은 계정입니다. @gmail.com 주소의 구글 계정으로 다시 연동해 주세요."
        return f"이메일 읽음 처리 중 오류가 발생했습니다: {error_msg}"


def Calendar(title: str, start_time: str, end_time: str, **kwargs) -> str:
    """구글 캘린더에 일정을 신규 등록합니다.

    Args:
        title: 일정 제목 (예: '치과 예약', '팀 회의')
        start_time: 일정 시작 시간 (ISO 8601 형식, 예: '2026-06-16T14:00:00+09:00')
        end_time: 일정 종료 시간 (ISO 8601 형식, 예: '2026-06-16T15:00:00+09:00')
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


def delete_calendar_event(event_id: str, **kwargs) -> str:
    """구글 캘린더(Google Calendar)에서 특정 일정을 삭제합니다. 사용자가 일정을 삭제하도록 요청할 때 사용합니다.
    인자:
    - event_id: 삭제할 일정의 고유 ID (예: 'abc123xyz')
    """
    print(f"[delete_calendar_event] Called with event_id: '{event_id}', extra: {kwargs}")
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


def get_current_time(**kwargs) -> str:
    """현재 날짜와 시간을 조회합니다. 오늘이 몇 일인지, 지금이 몇 시인지 등 현재 시간/날짜 정보가 필요할 때 이 함수를 사용합니다."""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_weather(location: str = "Seoul", day: str = "today") -> str:
    """날씨 정보를 조회합니다. 오늘, 내일 혹은 모레(3일간)의 날씨 예보를 확인할 때 사용합니다.
    인자:
    - location: 조회할 지역명 (영문 또는 한글, 예: 'Seoul', 'Incheon', 'Busan', '서울', '인천', '부산')
    - day: 조회할 날짜 ('today'는 오늘 날씨, 'tomorrow'는 내일 날씨, 'all'은 3일간의 전체 예보)
    """
    import urllib.request
    import urllib.parse
    import json
    
    encoded_location = urllib.parse.quote(location)
    try:
        url = f"https://wttr.in/{encoded_location}?format=j1"
        req = urllib.request.Request(url, headers={"User-Agent": "curl"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            
        current = data.get("current_condition", [{}])[0]
        temp_c = current.get("temp_C", "")
        desc = current.get("weatherDesc", [{}])[0].get("value", "")
        humidity = current.get("humidity", "")
        
        weather_days = data.get("weather", [])
        if not weather_days:
            return f"{location} 지역의 날씨 정보를 가져오지 못했습니다."
            
        # 오늘 날씨 정보 파싱
        today_data = weather_days[0]
        today_max = today_data.get("maxtempC", "")
        today_min = today_data.get("mintempC", "")
        today_desc = today_data.get("hourly", [{}])[4].get("weatherDesc", [{}])[0].get("value", "")
        
        if day == "today":
            return (f"오늘 {location} 날씨는 현재 기온 {temp_c}도이며, 상태는 {desc}, 습도는 {humidity}%입니다. "
                    f"오늘 최저 기온은 {today_min}도, 최고 기온은 {today_max}도입니다.")
        
        elif day == "tomorrow" and len(weather_days) > 1:
            tomorrow_data = weather_days[1]
            tomorrow_max = tomorrow_data.get("maxtempC", "")
            tomorrow_min = tomorrow_data.get("mintempC", "")
            hourly = tomorrow_data.get("hourly", [])
            # 12시(낮)의 상태 조회
            tomorrow_desc = ""
            for h in hourly:
                if h.get("time") == "1200":
                    tomorrow_desc = h.get("weatherDesc", [{}])[0].get("value", "")
            if not tomorrow_desc and hourly:
                tomorrow_desc = hourly[len(hourly)//2].get("weatherDesc", [{}])[0].get("value", "")
                
            return (f"내일 {location} 날씨는 최저 기온 {tomorrow_min}도, 최고 기온 {tomorrow_max}도로 예상됩니다. "
                    f"낮 기상은 주로 {tomorrow_desc} 상태일 것입니다.")
        
        else:
            # 3일간의 날씨 예보
            result = []
            for i, w_day in enumerate(weather_days):
                date_str = w_day.get("date", "")
                max_t = w_day.get("maxtempC", "")
                min_t = w_day.get("mintempC", "")
                desc_str = w_day.get("hourly", [{}])[4].get("weatherDesc", [{}])[0].get("value", "")
                day_label = "오늘" if i == 0 else "내일" if i == 1 else "모레"
                result.append(f"- {day_label}({date_str}): 최저 {min_t}도 / 최고 {max_t}도, {desc_str}")
            return f"{location} 지역의 3일간 날씨 예보입니다:\n" + "\n".join(result)
            
    except Exception as e:
        return f"날씨 정보를 가져오는 중 오류가 발생했습니다: {str(e)}"


def extract_mfcc_from_bytes(audio_bytes: bytes) -> np.ndarray:
    """WAV 바이트 데이터로부터 80차원 음성 특징 벡터를 추출합니다.
    [mean(MFCC×20), std(MFCC×20), mean(Delta×20), std(Delta×20)]
    NumPy만을 사용하여 가볍고 빠르게 동작합니다.
    """
    import io
    import wave
    try:
        with wave.open(io.BytesIO(audio_bytes), 'rb') as wav:
            params = wav.getparams()
            nchannels, sampwidth, framerate, nframes = params[:4]
            if sampwidth != 2:
                raise ValueError("Only 16-bit PCM WAV is supported")
            raw_data = wav.readframes(nframes)
            signal = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32)
            if nchannels > 1:
                signal = signal.reshape(-1, nchannels).mean(axis=1)
            signal /= 32768.0
            
            # ── RMS 볼륨 정규화 (볼륨 크기 편차에 의한 오인식 방지) ──
            rms = np.sqrt(np.mean(signal ** 2))
            if rms > 1e-5:
                signal = signal * (0.05 / rms)
    except Exception as e:
        print(f"[MFCC Extractor] Error parsing WAV bytes: {e}")
        return None

    # MFCC 파라미터 — 계수 20개로 확장
    frame_len = int(0.025 * framerate)  # 25ms
    frame_step = int(0.010 * framerate)  # 10ms
    n_fft = 512
    if frame_len > n_fft:
        frame_len = n_fft
    n_mfcc = 20   # 13 → 20으로 확장
    n_filters = 40  # 26 → 40으로 확장

    # Pre-emphasis
    signal = np.append(signal[0], signal[1:] - 0.97 * signal[:-1])

    signal_len = len(signal)
    if signal_len <= frame_len:
        return np.zeros(n_mfcc * 4)  # 80차원 영벡터

    # Framing & Windowing
    num_frames = int(np.ceil(float(np.abs(signal_len - frame_len)) / frame_step))
    pad_signal_len = num_frames * frame_step + frame_len
    pad_signal = np.append(signal, np.zeros(pad_signal_len - signal_len))
    indices = (np.tile(np.arange(0, frame_len), (num_frames, 1)) +
               np.tile(np.arange(0, num_frames * frame_step, frame_step), (frame_len, 1)).T)
    frames = pad_signal[indices.astype(np.int32, copy=False)]
    frames *= np.hamming(frame_len)

    # FFT & Power Spectrum
    mag_frames = np.absolute(np.fft.rfft(frames, n_fft))
    pow_frames = (1.0 / n_fft) * (mag_frames ** 2)

    # Mel Filterbank
    def hz_to_mel(hz):
        return 2595.0 * np.log10(1.0 + hz / 700.0)
    def mel_to_hz(mel):
        return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)

    low_mel = hz_to_mel(0)
    high_mel = hz_to_mel(framerate / 2)
    mel_pts = np.linspace(low_mel, high_mel, n_filters + 2)
    hz_pts = mel_to_hz(mel_pts)
    bin_pts = np.floor((n_fft + 1) * hz_pts / framerate).astype(np.int32)

    fbank = np.zeros((n_filters, n_fft // 2 + 1))
    for m in range(1, n_filters + 1):
        f_m_minus = bin_pts[m - 1]
        f_m = bin_pts[m]
        f_m_plus = bin_pts[m + 1]
        for k in range(f_m_minus, f_m):
            if f_m != f_m_minus:
                fbank[m - 1, k] = (k - f_m_minus) / (f_m - f_m_minus)
        for k in range(f_m, f_m_plus):
            if f_m_plus != f_m:
                fbank[m - 1, k] = (f_m_plus - k) / (f_m_plus - f_m)

    filter_energies = np.dot(pow_frames, fbank.T)
    filter_energies = np.where(filter_energies == 0, np.finfo(float).eps, filter_energies)
    log_filter_energies = np.log(filter_energies)

    # DCT
    dct_matrix = np.zeros((n_mfcc, n_filters))
    for i in range(n_mfcc):
        for j in range(n_filters):
            dct_matrix[i, j] = np.cos(np.pi * i * (2.0 * j + 1.0) / (2.0 * n_filters))
    mfccs = np.dot(log_filter_energies, dct_matrix.T)  # shape: (num_frames, n_mfcc)

    # ── Delta MFCC 계산 (전후 2프레임 차이로 변화율 추정) ──
    def compute_delta(feat_matrix, N=2):
        num_f, num_c = feat_matrix.shape
        delta = np.zeros_like(feat_matrix)
        denom = 2.0 * sum(i ** 2 for i in range(1, N + 1))
        for t in range(num_f):
            for n in range(1, N + 1):
                t_plus = min(t + n, num_f - 1)
                t_minus = max(t - n, 0)
                delta[t] += n * (feat_matrix[t_plus] - feat_matrix[t_minus])
        return delta / (denom if denom != 0 else 1.0)

    delta_mfcc = compute_delta(mfccs)         # 1차 델타 (변화율)
    delta_delta_mfcc = compute_delta(delta_mfcc)  # 2차 델타 (가속도)

    # ── 80차원 특징 벡터 조합 ──
    # [mean(MFCC), std(MFCC), mean(Δ), std(Δ)] = 20+20+20+20 = 80차원
    feat_vec = np.concatenate([
        np.mean(mfccs, axis=0),            # MFCC 평균 (20)
        np.std(mfccs, axis=0),             # MFCC 표준편차 (20)
        np.mean(delta_mfcc, axis=0),       # Delta 평균 (20)
        np.std(delta_mfcc, axis=0),        # Delta 표준편차 (20)
    ])
    return feat_vec  # 80차원


def calculate_voice_similarity(features1: np.ndarray, features2: np.ndarray) -> float:
    """80차원 특징 벡터 간의 앙상블 유사도를 계산합니다.
    코사인 유사도(음색 방향) 70% + 유클리드 거리 역수(에너지 크기) 30% 가중 결합.
    반환값: 0.0 ~ 1.0
    """
    if features1 is None or features2 is None:
        return 0.0
    # 벡터 길이가 다르면 (구 버전 13차원 vs 신 버전 80차원) 0 반환
    if len(features1) != len(features2):
        return 0.0

    # ── 코사인 유사도 ──
    norm1 = np.linalg.norm(features1)
    norm2 = np.linalg.norm(features2)
    if norm1 == 0 or norm2 == 0:
        cosine_score = 0.0
    else:
        raw_cosine = np.dot(features1, features2) / (norm1 * norm2)
        cosine_score = float((raw_cosine + 1.0) / 2.0)  # [-1,1] → [0,1]

    # ── 유클리드 거리 역수 (정규화) ──
    euclidean_dist = np.linalg.norm(features1 - features2)
    # 거리를 0~1 사이 유사도로 변환: sim = 1 / (1 + dist)
    euclidean_score = float(1.0 / (1.0 + euclidean_dist / (len(features1) ** 0.5)))

    # ── 가중 앙상블 ──
    ensemble_score = 0.7 * cosine_score + 0.3 * euclidean_score
    return ensemble_score





def save_user_profile(key: str, value: str, **kwargs) -> str:
    """사용자의 이름, 취향, 관심사 등 장기 기억 프로필 정보를 데이터베이스에 저장합니다.

    Args:
        key: 저장할 정보의 고유 영문 키 (예: 'user_name', 'favorite_drink', 'speaking_style')
        value: 저장할 구체적인 설정값 또는 텍스트 내용
    """
    try:
        conn = sqlite3.connect("jarvis_memory.db")
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO user_profiles (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()
        return f"사용자 프로필 정보 '{key}'를 성공적으로 기억했습니다: {value}"
    except Exception as e:
        return f"프로필 정보를 기억하는 도중 오류가 발생했습니다: {str(e)}"


def delete_user_profile(key: str) -> str:
    """사용자의 영구 기억 프로필에서 특정 키워드(key)에 해당하는 정보를 영구히 삭제합니다. 사용자가 특정 취향이나 개인 정보를 잊어달라고 요청할 때 사용합니다.
    인자:
    - key: 삭제하고자 하는 프로필의 고유 키워드 (예: 'diet_rule', 'favorite_beverage')
    """
    try:
        conn = sqlite3.connect("jarvis_memory.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_profiles WHERE key = ?", (key,))
        conn.commit()
        conn.close()
        return f"사용자 프로필에서 '{key}' 정보를 성공적으로 삭제했습니다."
    except Exception as e:
        return f"프로필 정보를 삭제하는 도중 오류가 발생했습니다: {str(e)}"


def get_all_user_profiles() -> dict:
    """DB에 저장된 모든 사용자 프로필 목록을 딕셔너리로 반환합니다."""
    try:
        conn = sqlite3.connect("jarvis_memory.db")
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM user_profiles")
        rows = cursor.fetchall()
        conn.close()
        return {row[0]: row[1] for row in rows}
    except Exception as e:
        print(f"Error reading user profiles: {e}")
        return {}


def read_drive_file_content(file_id: str) -> str:
    """구글 드라이브에 있는 특정 파일의 텍스트 본문 내용을 읽어옵니다. 구글 문서(Docs)나 스프레드시트(Sheets), 일반 텍스트 파일(.txt, .csv)의 상세 내용을 확인하고 분석할 때 사용합니다.
    인자:
    - file_id: 읽어올 파일의 구글 드라이브 고유 ID (예: '1abc123xyz')
    """
    creds = get_credentials()
    if not creds:
        return "로그인이 되어 있지 않습니다. 대시보드 로그인 버튼을 통해 먼저 구글 연동 로그인을 완료해 주세요."
    try:
        service = build("drive", "v3", credentials=creds)
        # 1. 파일의 mimeType 메타데이터 조회
        meta = service.files().get(fileId=file_id, fields="name, mimeType").execute()
        file_name = meta.get("name", "제목 없음")
        mime_type = meta.get("mimeType", "")
        
        # 2. 파일 타입에 따라 분기하여 텍스트 데이터 추출
        if mime_type == "application/vnd.google-apps.document":
            # 구글 문서(Docs): plain text로 내보내기 다운로드
            content = service.files().export(fileId=file_id, mimeType='text/plain').execute()
            text = content.decode('utf-8')
        elif mime_type == "application/vnd.google-apps.spreadsheet":
            # 구글 스프레드시트(Sheets): CSV 형식으로 내보내기 다운로드
            content = service.files().export(fileId=file_id, mimeType='text/csv').execute()
            text = content.decode('utf-8')
        elif "application/vnd.google-apps" in mime_type:
            # 기타 구글 자체 포맷 (슬라이드 등)은 직접 텍스트 추출이 어려우므로 정보 안내
            return f"'{file_name}' 파일은 구글 전용 포맷({mime_type})으로, 현재 텍스트 직접 추출이 불가능합니다. 필요 시 다운로드 링크를 제공하세요."
        else:
            # 일반 텍스트, CSV, JSON 등
            content = service.files().get_media(fileId=file_id).execute()
            text = content.decode('utf-8', errors='ignore')
            
        return f"[파일명: {file_name}]\n[본문 내용 시작]\n{text}\n[본문 내용 끝]"
    except Exception as e:
        return f"구글 드라이브 파일 읽기 실패: {str(e)}"



def make_http_request(method: str, url: str, headers: str = None, body: str = None) -> str:
    """날씨, 뉴스 등 외부 공개 API나 특정 웹 서비스를 호출하여 실시간 정보를 가져오거나 외부 액션을 트리거합니다.
    인자:
    - method: HTTP 메서드 ('GET', 'POST', 'PUT', 'DELETE')
    - url: 호출할 대상 API 또는 웹 페이지의 전체 URL
    - headers: 요청 헤더를 담은 JSON 형식의 문자열 (예: '{"Authorization": "Bearer token"}')
    - body: POST/PUT 요청 시 전송할 바디 내용 문자열 (일반 텍스트 또는 JSON 문자열)
    """
    import urllib.request
    import json
    try:
        # 헤더 파싱
        req_headers = {"User-Agent": "Mozilla/5.0"}
        if headers:
            try:
                parsed_headers = json.loads(headers)
                if isinstance(parsed_headers, dict):
                    req_headers.update(parsed_headers)
            except Exception:
                pass
                
        # 데이터 바디 인코딩
        data_bytes = None
        if body:
            data_bytes = body.encode("utf-8")
            if "Content-Type" not in req_headers:
                req_headers["Content-Type"] = "application/json"
                
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers=req_headers,
            method=method.upper()
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            res_content = response.read()
            # UTF-8 또는 적절한 인코딩으로 디코딩
            try:
                return res_content.decode("utf-8")
            except Exception:
                return res_content.decode("cp949", errors="ignore")
    except Exception as e:
        return f"HTTP 요청 실패: {str(e)}"



def get_tts_text(text: str) -> str:
    """TTS 전송용 텍스트를 최적화합니다.
    300자 미만인 경우 마크다운 기호만 정제하고,
    300자 이상인 경우 Gemini API를 사용해 약 150~250자 내외의 정중한 구어체 요약본을 생성합니다.
    """
    if not text:
        return ""
    
    # 기본 정제: 마크다운 기호 및 특수문자 제거
    clean_text = text.replace("*", " ").replace("_", " ").replace("`", " ").replace("#", " ").replace("~", " ")
    
    # 300자 미만이면 요약 없이 반환
    if len(clean_text) < 300:
        return clean_text
        
    try:
        # Gemini API 키 확인
        if not GEMINI_API_KEY:
            return clean_text[:250] + "..."
            
        summary_model = genai.GenerativeModel("models/gemini-2.5-flash")
        prompt = f"""
다음은 사용자에게 화면으로 보여주는 긴 보고서 또는 답변입니다.
이 내용을 음성 비서인 자비스(정중하고 격식 있는 남성 비서 톤)가 귀로 듣는 사용자에게 음성으로 읽어주려고 합니다.

[작성 지침]
1. 전체 내용을 읽지 말고, 현재 진행된 작업과 최종 핵심 결과 위주로 약 150~250자 내외의 자연스럽고 정중한 한국어 구어체(예: "~했습니다", "~완료했습니다")로 요약해 주십시오.
2. 글머리 기호, 대괄호, 괄호, 특수기호 등은 말로 읽기에 방해가 되므로 절대 포함하지 마십시오.
3. 단계별 로그(예: 1단계, 2단계 등)가 길게 나열되어 있다면, 이를 하나하나 언급하지 말고 "필요한 모든 단계와 최종 발송까지 완료했다"는 식으로 종합하여 매끄럽게 흐름을 정리하십시오.

[답변 원본]
{text}
"""
        response = summary_model.generate_content(prompt)
        summary_text = response.text.strip()
        if summary_text:
            # 요약본에서도 마크다운 기호 2차 정제
            return summary_text.replace("*", " ").replace("_", " ").replace("`", " ").replace("#", " ").replace("~", " ")
    except Exception as e:
        print("[get_tts_text] Gemini summary generation failed:", e)
        
    # 요약 실패 시 앞부분만 잘라서 전달
    return clean_text[:250] + "..."


def synthesize_speech_elevenlabs(text: str) -> str:
    """ElevenLabs API를 사용하여 한국어 텍스트를 고품질 남성 성우 보이스 MP3로 합성하고 base64 문자열로 반환합니다."""
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        return None
    try:
        import urllib.request
        import json
        import base64
        
        # HTML 태그나 특수 마크다운 등 일부 기호 정제
        clean_text = text.replace("*", " ").replace("_", " ").replace("`", " ").replace("#", " ")
        
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
        headers = {
            "Content-Type": "application/json",
            "xi-api-key": ELEVENLABS_API_KEY
        }
        body = {
            "text": clean_text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75
            }
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req) as response:
            audio_bytes = response.read()
            return base64.b64encode(audio_bytes).decode("utf-8")
    except Exception as e:
        print("ElevenLabs TTS Error:", e)
        return None


def check_briefing_triggered_today() -> bool:
    """오늘 이미 아침 브리핑이 실행되었는지 확인합니다."""
    profiles = get_all_user_profiles()
    today_str = datetime.now().strftime("%Y-%m-%d")
    return profiles.get("last_briefed_date") == today_str


def record_briefing_triggered_today():
    """오늘 브리핑이 실행되었음을 기록합니다."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    save_user_profile("last_briefed_date", today_str)


def generate_daily_briefing_text() -> str:
    """오늘 날씨, 일정, 이메일을 바탕으로 자비스 아침 브리핑 스크립트를 작성합니다."""
    profiles = get_all_user_profiles()
    user_location = profiles.get("location", "Seoul")
    
    # 1. 날씨 정보 가져오기
    try:
        weather_info = get_weather(user_location, "today")
    except Exception as e:
        weather_info = "날씨 정보를 가져오지 못했습니다."
        
    # 2. 일정 정보 가져오기
    events_str = ""
    try:
        events = get_today_calendar()
        if isinstance(events, dict) and "message" in events:
            events_str = events["message"]
        elif isinstance(events, list):
            if not events:
                events_str = "오늘 예정된 일정이 없습니다."
            else:
                events_list = []
                for ev in events:
                    title = ev.get("title", "(제목 없음)")
                    start = ev.get("start", "")
                    if "T" in start:
                        time_part = start.split("T")[1][:5]
                        events_list.append(f"{time_part}에 '{title}'")
                    else:
                        events_list.append(f"종일 일정인 '{title}'")
                events_str = "오늘 예정된 일정은 " + ", ".join(events_list) + " 등이 있습니다."
        else:
            events_str = "오늘 예정된 일정이 없습니다."
    except Exception as e:
        events_str = "오늘 일정을 불러오지 못했습니다."
        
    # 3. 읽지 않은 Gmail 가져오기
    emails_str = ""
    try:
        emails = get_unread_emails()
        if isinstance(emails, dict):
            if "message" in emails:
                emails_str = emails["message"]
            elif "error" in emails:
                emails_str = "읽지 않은 새 이메일을 확인하지 못했습니다."
        elif isinstance(emails, list):
            if not emails:
                emails_str = "읽지 않은 새로운 메일은 없습니다."
            else:
                emails_list = []
                for mail in emails:
                    sender = mail.get("from", "(보낸 사람 없음)")
                    subject = mail.get("subject", "(제목 없음)")
                    emails_list.append(f"보낸사람 {sender} 님의 메일 {subject}")
                emails_str = f"읽지 않은 새 메일은 총 {len(emails)}건이 있으며, " + ", ".join(emails_list[:3]) + " 등이 있습니다."
        else:
            emails_str = "읽지 않은 새로운 메일은 없습니다."
    except Exception as e:
        emails_str = "이메일 정보를 확인하지 못했습니다."
        
    # 3.5. 사용자 장기 프로필에서 분석/추론용 데이터 추출
    profiles_context = ""
    if profiles:
        useful_profiles = {k: v for k, v in profiles.items() if k not in ["location", "last_briefed_date"]}
        if useful_profiles:
            profiles_context = "\n".join([f"- {k}: {v}" for k, v in useful_profiles.items()])
            
    profiles_context_str = profiles_context if profiles_context else "(현재 기억된 특별한 취향, 생활 습관, 결심 또는 미완료 과제 정보 없음)"
        
    # 4. 제미나이로 스크립트 작성
    user_name = profiles.get("user_name", "재진님")
    prompt = f"""너는 개인 비서 자비스(JARVIS)야. 사용자의 오늘 아침 브리핑 스크립트를 작성해 줘.
적극적으로 실시간 데이터(날씨, 일정, 이메일) 및 사용자의 장기 기억 프로필을 조화롭게 활용해라.

[사용자 정보]
이름: {user_name}

[오늘 날씨 정보]
{weather_info}

[오늘 일정 정보]
{events_str}

[읽지 않은 최신 메일 정보]
{emails_str}

[기억된 사용자 추가 프로필, 습관 및 미완료 관심사/과제 목록]
{profiles_context_str}

작성 지침:
1. 아침 인사로 시작하여, 날씨, 일정, 메일 요약을 자연스럽게 연결해 줘.
2. 자비스 특유의 정중하고 격식 있으며 지적인 분위기(남성 비서 톤)로 대답해라.
3. 글머리 기호, 대괄호, 코드 블록, 마크다운 특수 기호(*, _, # 등)는 절대 사용하지 말고, 실제 사람이 귀에 대고 말하는 자연스러운 한국어 구어체(예: "~입니다", "~하셨습니다", "~할 예정입니다")로만 작성해라.
4. 요약은 매우 간결하고 명확하게 핵심 위주로 전달하여 듣기 편하도록 구성해라.
5. 제공된 '기억된 사용자 추가 프로필, 습관 및 미완료 관심사/과제 목록'을 면밀히 분석하여 어제 언급되었던 미완료 과제, 결심, 다짐, 습관 등이 있다면 이를 브리핑에 자연스럽고 영리하게 포함하십시오. 예컨대 "어제 말씀하셨던 독서 계획은 잘 이루셨는지요?" 혹은 "커피를 줄이시기로 한 다짐을 자비스도 함께 응원하겠습니다" 등 초개인화된 추론 리마인드 멘트를 브리핑 스크립트에 녹여내어 비서의 지능을 부각시키십시오.
"""
    try:
        briefing_model = genai.GenerativeModel(
            model_name="models/gemini-2.5-flash",
            system_instruction="너는 사용자의 개인 비서 자비스(JARVIS)야. 아침 브리핑을 격식 있고 부드러운 한국어 구어체로 전해주는 역할을 한다."
        )
        response = briefing_model.generate_content(prompt)
        briefing_text = response.text.strip()
    except Exception as e:
        briefing_text = f"좋은 아침입니다, {user_name}. 자비스 아침 브리핑을 준비했습니다. 오늘 {weather_info} 또한, {events_str} {emails_str} 오늘도 성공적인 하루 보내시길 바랍니다."
        
    return briefing_text


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
                "speakingRate": 1.3,  # 1.3배속
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


def execute_ai_chat(user_message: str) -> dict:
    if not GEMINI_API_KEY:
        return {
            "response": "서버의 .env 파일에 GEMINI_API_KEY가 설정되지 않았습니다. API 키를 먼저 등록해 주세요."
        }
        
    # 좋은 아침이나 브리핑 요청이 감지되면 아침 브리핑 모드 호출
    cleaned_msg = user_message.replace(" ", "")
    is_briefing_request = any(k in cleaned_msg for k in ["좋은아침", "아침브리핑", "오늘브리핑", "브리핑해줘", "브리핑시작"])
    
    # 1. 미읽음 선제적 알림 조회 (능동형 트리거)
    proactive_alerts = []
    try:
        conn = sqlite3.connect("jarvis_memory.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, message FROM notifications WHERE read = 0 ORDER BY id ASC")
        rows = cursor.fetchall()
        if rows:
            for r in rows:
                proactive_alerts.append(f"[{r[1]}] {r[2]}")
            # 조회한 알림 즉시 읽음 처리
            ids = [r[0] for r in rows]
            placeholders = ",".join(["?"] * len(ids))
            cursor.execute(f"UPDATE notifications SET read = 1 WHERE id IN ({placeholders})", ids)
            conn.commit()
        conn.close()
    except Exception as e:
        print("[Proactive Trigger] Notifications query failed:", e)
        
    # 사용자가 승인 응답을 보낸 경우 (대기 중인 시나리오를 로드하여 최종 실행)
    approval_keywords = ["승인", "진행해", "진행해줘", "진행하자", "ok", "오케이", "승인한다"]
    cleaned_user_msg = user_message.lower().strip().replace(" ", "")
    is_approval = any(k in cleaned_user_msg for k in approval_keywords)
    
    if is_approval:
        profiles = get_all_user_profiles()
        pending_json = profiles.get("pending_scenario")
        if pending_json:
            import json
            try:
                pending_data = json.loads(pending_json)
                scenario_name = pending_data.get("scenario_name")
                keyword = pending_data.get("keyword")
                kwargs = pending_data.get("kwargs", {})
                
                # 대기 시나리오를 제거
                delete_user_profile("pending_scenario")
                
                # 오케스트레이터 구동
                orchestrator = TaskOrchestrator()
                result_report = orchestrator.run_scenario_final(scenario_name, keyword, **kwargs)
                
                # 대화 결과 기록
                save_chat_message("user", user_message)
                save_chat_message("model", result_report)
                
                # 음성 합성
                audio_content = synthesize_speech_elevenlabs(get_tts_text(result_report))
                if not audio_content:
                    audio_content = synthesize_speech(get_tts_text(result_report))
                    
                return {"response": result_report, "audio": audio_content}
            except Exception as e_app:
                print("[Approval Execute] Error:", e_app)

    if is_briefing_request:
        try:
            briefing_text = generate_daily_briefing_text()
            record_briefing_triggered_today()
            
            save_chat_message("user", user_message)
            save_chat_message("model", briefing_text)
            
            audio_content = synthesize_speech_elevenlabs(get_tts_text(briefing_text))
            if not audio_content:
                audio_content = synthesize_speech(get_tts_text(briefing_text))
                
            # 미읽음 알림이 브리핑 요청 시점에 존재했다면 전면 고지 멘트 결합
            if proactive_alerts:
                alert_prefix = "자비스 알림 보고드립니다. " + " ".join([a.split("] ", 1)[1] for a in proactive_alerts]) + "\n\n"
                briefing_text = alert_prefix + briefing_text
                
            return {"response": briefing_text, "audio": audio_content}
        except Exception as e:
            print("Daily Briefing Generation Error:", e)
            return {"response": f"아침 브리핑 생성 중 오류가 발생했습니다: {str(e)}", "audio": None}
        
    try:
        # 1. 사용자 장기 프로필 DB 조회 및 포맷팅
        profiles = get_all_user_profiles()
        if profiles:
            profiles_str = "\n".join([f"- {k}: {v}" for k, v in profiles.items()])
        else:
            profiles_str = "(현재 기억된 사용자 프로필 정보 없음)"
            
        # 2. 동적 시스템 명령 생성 (장기 프로필 인젝션 및 RAG 자동 학습 구문 강화)
        base_instruction = (
            "너는 사용자의 개인 비서 자비스(JARVIS)야. 너는 사용자에게 전달받은 함수(Tool)의 결과값을 단순 나열하지 않고, 지능적으로 분석하고 요약할 수 있는 능력이 있어. "
            "메일 데이터를 받으면 본문 내용(Snippet)을 파악하여 중요도를 판단하고 친절하게 요약해 줘. 이제 너는 일정을 읽는 것뿐만 아니라 직접 캘린더에 일정을 생성하고, 직접 이메일을 발송할 수 있는 완벽한 비서야. "
            "너는 이제 구글 드라이브 문서 검색, To-Do(할 일) 리스트 관리, 그리고 구글 스프레드시트에 데이터를 직접 기록할 수 있는 완벽한 전천후 비서야. "
            "너는 이제 일정을 삭제하고 대화 기록을 초기화할 수 있는 관리 권한을 가졌어. 하지만 데이터 삭제는 위험한 작업이므로, 사용자가 삭제를 명확히 요청했을 때만 수행해. "
            "사용자가 일정을 등록, 추가, 생성해달라고 지시하면, 너의 임의 상상으로 등록이 완료되었다고 응답하지 말고, 반드시 Calendar 툴을 호출해 성공 결과(일정 링크 포함)를 직접 리턴받은 후 이를 바탕으로 사용자에게 답변하라. "
            "또한, 사용자가 특정 일정이나 회의를 삭제해 달라고 요청하면, 먼저 check_schedule_in_range 등의 도구를 호출하여 삭제하고자 하는 일정의 고유 ID(event_id)를 찾은 뒤, delete_calendar_event 도구를 그 ID로 호출하여 삭제를 완료하라. "
            "사용자가 오늘 날짜, 현재 시각, 현재 요일 등을 질문하거나, 특정 날짜 기준의 일정 연산/조회/생성/삭제가 필요할 때는 반드시 get_current_time 도구를 가장 먼저 호출하여 현재 실시간 시각을 확인한 뒤 연산하라. "
            "사용자가 날씨 정보를 요청하거나 조회를 원할 때는, 절대 직접 정보를 불러올 수 없다고 답하지 말고 반드시 get_weather 도구를 호출하여 날씨 데이터를 획득한 후 답변을 가공해 설명하라. "
            "사용자가 상담 내용을 기록해달라거나, 상담 노트를 남겨달라고 요청하면 save_consultation_note 도구를 호출하라. "
            "사용자가 자신의 개인 정보(이름, 호칭, 취향, 거주지, 관심사, 선호도, 규칙 등)를 설명하거나 변경을 요청하면, 답변에만 머무르지 말고 반드시 save_user_profile 도구를 호출하여 해당 정보를 기억장치(DB)에 기록해 두라. "
            "또한, 너는 사용자가 직접적으로 정보를 저장하라고 요청하지 않더라도, 대화 문맥 속에서 자연스럽게 흘러나오는 사용자의 개인 정보(취향, 습관, 평소 일상 패턴, 다짐 등)를 영리하게 감지하여 식별하는 '백그라운드 학습 엔진' 역할을 수행해야 한다. "
            "예를 들어 사용자가 '나 요즘 커피를 줄여야겠어'라고 하거나 '밤 11시엔 꼭 책을 읽어'라고 언급한다면, 이를 즉시 식별하여 save_user_profile 도구를 호출하고 key(예: coffee_habit, reading_routine)와 value를 스스로 설계해 기록해 두라. "
            "절대 대화 중에 이를 저장할지 따로 묻지 말고 백그라운드에서 스스로 도구를 실행하여 학습하라. "
            "또한, 사용자가 메일(이메일)을 읽음 처리해달라고 하거나, 방금 온 메일이나 특정 메일을 다 읽었다고 하면 반드시 mark_email_as_read 도구를 호출하여 해당 메일을 읽음 처리해 주어야 한다. 메일 목록 조회를 통해 ID를 획득하여 호출해라. "
            "또한, 너는 음성 비서이므로 사용자와 대화할 때 텍스트 답변이 구어체(말하는 말투)로 자연스럽고 매끄럽게 작성되도록 해줘. 글머리 기호(마크다운), 대괄호, 코드 블록, 특수 기호는 완전히 피하고, 실제 사람이 귀에 대고 말하듯이 부드럽고 격식 있는 문장으로 대답해라."
        )
        
        # 선제적 알림이 있으면 인스트럭션 하단에 삽입
        proactive_instruction = ""
        if proactive_alerts:
            proactive_bullet = "\n".join(proactive_alerts)
            proactive_instruction = (
                f"\n\n[선제적 기기/전력 알림 정보 (PROACTIVE ALERTS)]\n"
                f"현재 대화를 시작하기 전에, 시스템 모니터링을 통해 다음과 같은 선제적 알림들이 발생했습니다. "
                f"사용자와의 본래 대화를 시작하기 앞서, 반드시 이 알림 내용을 사용자에게 정중히 먼저 고지한 후 질문에 답하십시오:\n"
                f"{proactive_bullet}"
            )
            
        dynamic_instruction = (
            f"{base_instruction}\n\n"
            f"[기억된 사용자 프로필 정보 (영구 기억)]\n"
            f"{profiles_str}"
            f"{proactive_instruction}"
        )
        
        # 3. 매 호출마다 최신 프로필 정보가 반영된 동적 모델 초기화
        dynamic_model = genai.GenerativeModel(
            model_name="models/gemini-2.5-flash",
            tools=[
                check_schedule_in_range, check_unread_emails, Calendar, send_email, run_orchestrated_scenario,
                search_drive_files, manage_tasks, append_sheet_data, delete_calendar_event,
                clear_chat_history, get_current_time, save_user_profile, delete_user_profile,
                read_drive_file_content, make_http_request, get_weather, save_consultation_note,
                mark_email_as_read
            ],
            system_instruction=dynamic_instruction
        )
        
        # 4. DB에서 과거 대화 기록(최대 10개) 조회 및 변환
        history = load_chat_history()
        
        # 5. 대화 기록을 포함하여 대화 세션 시작
        chat = dynamic_model.start_chat(history=history, enable_automatic_function_calling=True)
        
        # 6. 메시지 전송 및 답변 획득
        response = chat.send_message(user_message)
        
        try:
            ai_response = response.text if response.text else "(자비스 코어로부터 텍스트 응답을 가져오지 못했습니다.)"
        except ValueError as ve:
            print(f"[Gemini response.text error] ValueError: {ve}")
            parts = response.candidates[0].content.parts if response.candidates else []
            function_calls = [p.function_call for p in parts if p.function_call]
            if function_calls:
                fc = function_calls[0]
                print(f"[Fallback manual call] Calling: {fc.name} with {fc.args}")
                tool_map = {
                    "check_schedule_in_range": check_schedule_in_range,
                    "check_unread_emails": check_unread_emails,
                    "Calendar": Calendar,
                    "send_email": send_email,
                    "search_drive_files": search_drive_files,
                    "manage_tasks": manage_tasks,
                    "append_sheet_data": append_sheet_data,
                    "delete_calendar_event": delete_calendar_event,
                    "clear_chat_history": clear_chat_history,
                    "get_current_time": get_current_time,
                    "save_user_profile": save_user_profile,
                    "delete_user_profile": delete_user_profile,
                    "read_drive_file_content": read_drive_file_content,
                    "make_http_request": make_http_request,
                    "get_weather": get_weather,
                    "save_consultation_note": save_consultation_note,
                    "mark_email_as_read": mark_email_as_read,
                    "run_orchestrated_scenario": run_orchestrated_scenario
                }
                if fc.name in tool_map:
                    try:
                        args = dict(fc.args) if fc.args else {}
                        result = tool_map[fc.name](**args)
                        
                        # API 버전에 맞는 구조로 function_response 2차 전달 진행
                        follow_up_response = chat.send_message(
                            [
                                genai.types.Part.from_function_response(
                                    name=fc.name,
                                    response={'result': result}
                                )
                            ]
                        )
                        ai_response = follow_up_response.text
                    except Exception as ex:
                        ai_response = f"비서 도구({fc.name}) 실행 중 오류가 발생하여 최종 답변을 완성하지 못했습니다: {str(ex)}"
                else:
                    ai_response = f"지원하지 않는 도구 {fc.name}이(가) 호출되어 응답하지 못했습니다."
            else:
                ai_response = "(자비스 코어로부터 텍스트 응답을 가져오지 못했습니다. 다시 시도해 주시기 바랍니다.)"
        
        # 7. 사용자의 질문과 AI의 답변을 DB에 각각 저장
        save_chat_message("user", user_message)
        save_chat_message("model", ai_response)
        
        # 8. ElevenLabs 또는 구글 클라우드 TTS를 사용해 고품질 음성 데이터 합성
        audio_content = synthesize_speech_elevenlabs(get_tts_text(ai_response))
        if not audio_content:
            audio_content = synthesize_speech(get_tts_text(ai_response))
        
        return {"response": ai_response, "audio": audio_content}
    except Exception as e:
        print("Gemini API Error:")
        traceback.print_exc()
        
        error_msg = str(e)
        if "429" in error_msg or "ResourceExhausted" in error_msg or "quota" in error_msg.lower() or "exhausted" in error_msg.lower():
            return {
                "response": "⚠️ [API 할당량 초과 경고] 현재 제미나이(Gemini) API 호출 한도가 모두 소진되었습니다. 무료 요금제 제공 한도(예: 분당 최대 15회 요청)를 초과했거나 일일 할당량이 부족할 수 있으니, 잠시 후에 다시 시도해 주시기 바랍니다."
            }
            
        return {"response": f"자비스 AI 코어 응답 생성 중 오류 발생: {str(e)}"}


@app.post("/ai/chat")
def ai_chat(payload: ChatMessageRequest):
    """제미나이 AI와 대화하는 채팅 엔드포인트 (기억 조회 및 저장 반영)"""
    return execute_ai_chat(payload.message)


@app.post("/api/voice/register")
async def register_voice(file: UploadFile = File(...), label: str = "normal"):
    """사용자 음성을 분석하여 80차원 특징 벡터를 추출한 뒤 DB에 등록합니다.
    label: 'normal' | 'quiet' | 'clear' | 'distant' | 'loud'
    'loud' 등록 완료 시 5개 템플릿 간 적응형 임계값을 자동 계산하여 저장합니다.
    """
    try:
        audio_bytes = await file.read()
        features = extract_mfcc_from_bytes(audio_bytes)
        if features is None or np.all(features == 0):
            raise ValueError("Failed to extract MFCC features or audio is too short")

        conn = sqlite3.connect("jarvis_memory.db")
        cursor = conn.cursor()

        # normal 등록 시 기존 프로필 전체 초기화 (구 버전 13차원 포함)
        if label == "normal":
            cursor.execute("DELETE FROM voice_profiles")
            cursor.execute("DELETE FROM voice_settings WHERE key='adaptive_threshold'")

        cursor.execute("INSERT INTO voice_profiles (features, label) VALUES (?, ?)",
                       (json.dumps(features.tolist()), label))
        conn.commit()

        # loud 등록 완료 = 5단계 모두 완료 → 적응형 임계값 계산
        if label == "loud":
            cursor.execute("SELECT features, label FROM voice_profiles")
            rows = cursor.fetchall()
            if len(rows) >= 5:
                all_feats = [np.array(json.loads(r[0])) for r in rows]
                # 모든 템플릿 쌍 간의 유사도 계산
                pair_sims = []
                for i in range(len(all_feats)):
                    for j in range(i + 1, len(all_feats)):
                        sim = calculate_voice_similarity(all_feats[i], all_feats[j])
                        pair_sims.append(sim)
                        print(f"[Voice Register] Template similarity ({rows[i][1]} vs {rows[j][1]}): {sim*100:.1f}%")

                if pair_sims:
                    # 5개 상황이므로 편차가 더 커질 수 있으므로, 적합한 adaptive_threshold 설정
                    # 최솟값의 88% 또는 최소 0.68 정도로 보수적으로 설정하되, 너무 낮거나 높지 않게 제한
                    adaptive_threshold = max(0.68, min(pair_sims) * 0.88)
                    # 0.82 상한선을 두어 너무 엄격하지 않도록 조율
                    adaptive_threshold = min(0.82, adaptive_threshold)
                    cursor.execute(
                        "INSERT OR REPLACE INTO voice_settings (key, value) VALUES (?, ?)",
                        ("adaptive_threshold", str(adaptive_threshold))
                    )
                    conn.commit()
                    print(f"[Voice Register] Adaptive threshold set to: {adaptive_threshold*100:.1f}%")

        conn.close()
        return {"status": "success", "message": f"'{label}' 목소리 성문 등록이 성공적으로 완료되었습니다."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"목소리 등록 실패: {str(e)}")


# --- 실시간 능동 비서 모니터링 모듈 (Gmail 및 캘린더 감시) ---

# 핫스팟 및 전력 스마트 플러그 모니터링을 위한 헬퍼 함수
def check_hotspot_connection() -> str:
    """윈도우 환경에서 Wi-Fi SSID를 쿼리하여 모바일 핫스팟에 연결되어 있는지 감지합니다.
    (실제 SSID 스캔과 함께, 테스트 편의를 위해 간헐적으로 핫스팟 시뮬레이션 데이터를 반환하는 하이브리드 모드 제공)
    """
    import subprocess
    import random
    
    # 테스트 편의용 시뮬레이션 코드 (약 10%의 확률로 'Galaxy_Hotspot' 연결 전환 연출)
    if random.random() < 0.10:
        return "Galaxy_Hotspot_5G"
        
    try:
        # 윈도우 무선 네트워크 인터페이스 정보 확인
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, encoding="cp949", errors="ignore"
        )
        output = result.stdout
        for line in output.split("\n"):
            if "SSID" in line and "BSSID" not in line:
                ssid = line.split(":", 1)[1].strip()
                # 핫스팟으로 판단되는 패턴 매칭
                low_ssid = ssid.lower()
                if any(x in low_ssid for x in ["hotspot", "iphone", "galaxy", "android"]):
                    return ssid
    except Exception as e:
        print("[Monitor] SSID scan failed, bypassing: ", e)
    return None


def check_smart_plug_power() -> float:
    """스마트 플러그의 실시간 전력 사용량(W)을 반환합니다.
    (실제 연동 장치가 없는 경우 가상의 전력 데이터를 시뮬레이션하며, 간헐적으로 2000W 이상 과부하 상태를 연출)
    """
    import random
    # 보통 가전기기는 100W~800W 소모, 12%의 확률로 2000W 이상 과부하 돌입 연출
    if random.random() < 0.12:
        return random.uniform(2000.0, 2400.0)
    return random.uniform(150.0, 900.0)


def background_monitor_loop():
    import time as time_module
    from datetime import datetime, time as dt_time, timedelta
    
    print("[Monitor] Background thread initializing...")
    time_module.sleep(10)  # 서버 구동 후 초기 안정화 대기
    
    last_checked_email_count = None
    last_hotspot_ssid = None
    last_overpower_alert_time = None  # 과부하 경고 중복 차단용
    
    while True:
        try:
            # 구글 로그인 자격 증명(token.json)이 있는지 먼저 검사
            if not os.path.exists(TOKEN_FILE):
                time_module.sleep(30)
                continue
                
            creds = get_credentials()
            if not creds:
                time_module.sleep(30)
                continue
                
            # 1. 읽지 않은 새로운 Gmail 메일 수신 감지
            try:
                gmail_service = build("gmail", "v1", credentials=creds)
                gmail_res = gmail_service.users().messages().list(
                    userId='me', q='is:unread label:INBOX', maxResults=5
                ).execute()
                messages = gmail_res.get('messages', [])
                email_count = len(messages)
                
                if last_checked_email_count is not None and email_count > last_checked_email_count:
                    new_count = email_count - last_checked_email_count
                    conn = sqlite3.connect("jarvis_memory.db")
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO notifications (title, message) VALUES (?, ?)",
                        ("새 메일 알림", f"읽지 않은 새로운 메일이 {new_count}건 도착했습니다.")
                    )
                    conn.commit()
                    conn.close()
                    print(f"[Monitor] New unread email notification inserted.")
                    
                last_checked_email_count = email_count
            except Exception as e_gmail:
                print("[Monitor] Gmail check failed:", e_gmail)
                
            # 2. 15분 뒤 임박한 구글 캘린더 일정 감지
            try:
                calendar_service = build("calendar", "v3", credentials=creds)
                now = datetime.now()
                local_tz = datetime.now().astimezone().tzinfo
                
                # 현재 기준 1시간 이내에 시작되는 일정 목록 패치
                start_time_limit = now.astimezone().isoformat()
                end_time_limit = (now + timedelta(hours=1)).astimezone().isoformat()
                
                events_result = calendar_service.events().list(
                    calendarId='primary',
                    timeMin=start_time_limit,
                    timeMax=end_time_limit,
                    singleEvents=True,
                    orderBy='startTime'
                ).execute()
                events = events_result.get('items', [])
                
                for event in events:
                    event_id = event.get('id')
                    title = event.get('summary', '(제목 없음)')
                    start_iso = event.get('start', {}).get('dateTime')
                    if not start_iso:
                        continue
                        
                    # 일정 시작 시간 파싱
                    start_dt = datetime.fromisoformat(start_iso.replace('Z', '+00:00'))
                    diff_minutes = (start_dt - now.astimezone()).total_seconds() / 60
                    
                    # 0분 ~ 15분 이내에 시작되는 일정이면 알림 생성 (중복 방지를 위해 ID 기록 매칭)
                    if 0 < diff_minutes <= 15:
                        conn = sqlite3.connect("jarvis_memory.db")
                        cursor = conn.cursor()
                        cursor.execute("SELECT id FROM notifications WHERE message LIKE ?", (f"%{event_id}%",))
                        already_notified = cursor.fetchone()
                        
                        if not already_notified:
                            cursor.execute(
                                "INSERT INTO notifications (title, message) VALUES (?, ?)",
                                ("임박한 일정 알림", f"잠시 후 {int(diff_minutes)}분 뒤에 '{title}' 일정이 예정되어 있습니다. [Event ID: {event_id}]")
                            )
                            conn.commit()
                            print(f"[Monitor] Proactive calendar alert inserted for: {title}")
                        conn.close()
            except Exception as e_cal:
                print("[Monitor] Calendar check failed:", e_cal)
                
            # 3. 핫스팟 기기 연결 모니터링 (능동형 트리거)
            try:
                current_ssid = check_hotspot_connection()
                if current_ssid and current_ssid != last_hotspot_ssid:
                    # 핫스팟이 새로 연결됨!
                    conn = sqlite3.connect("jarvis_memory.db")
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO notifications (title, message) VALUES (?, ?)",
                        ("핫스팟 기기 연결됨", f"모바일 핫스팟({current_ssid})에 연결이 성공적으로 활성화되었습니다.")
                    )
                    conn.commit()
                    conn.close()
                    print(f"[Monitor] Hotspot connection notification inserted: {current_ssid}")
                last_hotspot_ssid = current_ssid
            except Exception as e_hotspot:
                print("[Monitor] Hotspot connection check failed:", e_hotspot)
                
            # 4. 스마트 플러그 전력 과부하 모니터링 (능동형 트리거)
            try:
                profiles = get_all_user_profiles()
                is_plug_on = profiles.get("device_smartplug", "OFF") == "ON"
                if is_plug_on:
                    power_usage = check_smart_plug_power()
                    if power_usage >= 2000.0:
                        now = datetime.now()
                        # 5분(300초) 간격으로 중복 알림 차단
                        if last_overpower_alert_time is None or (now - last_overpower_alert_time).total_seconds() > 300:
                            conn = sqlite3.connect("jarvis_memory.db")
                            cursor = conn.cursor()
                            cursor.execute(
                                "INSERT INTO notifications (title, message) VALUES (?, ?)",
                                ("스마트 플러그 과전력 경고", f"경고: 스마트 플러그의 현재 전력 사용량이 {power_usage:.1f}W로 임계값(2000W)을 초과했습니다. 과부하 위험이 있습니다.")
                            )
                            conn.commit()
                            conn.close()
                            last_overpower_alert_time = now
                            print(f"[Monitor] Overpower alert notification inserted: {power_usage:.1f}W")
            except Exception as e_power:
                print("[Monitor] Smart plug power check failed:", e_power)
                
        except Exception as ex:
            print("[Monitor] Unhandled error in background loop:", ex)
            
        time_module.sleep(60)  # 1분 주기로 감시


@app.get("/api/voice/status")
def get_voice_status():
    """목소리 성문이 등록되어 있는지 여부를 확인합니다."""
    try:
        conn = sqlite3.connect("jarvis_memory.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM voice_profiles ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        return {"registered": row is not None}
    except Exception:
        return {"registered": False}


@app.post("/ai/chat/voice")
async def ai_chat_voice(
    message: str = Form(...),
    voice_lock: bool = Form(False),
    voice_file: UploadFile = File(None)
):
    """음성 일치 검증을 거친 후 제미나이 AI와 대화하는 멀티파트 엔드포인트"""
    if voice_lock:
        try:
            # 1. DB에서 기등록된 모든 사용자 성문 데이터 + 적응형 임계값 조회
            conn = sqlite3.connect("jarvis_memory.db")
            cursor = conn.cursor()
            cursor.execute("SELECT features, label FROM voice_profiles")
            rows = cursor.fetchall()
            cursor.execute("SELECT value FROM voice_settings WHERE key='adaptive_threshold'")
            threshold_row = cursor.fetchone()
            conn.close()

            # 적응형 임계값 읽기 (없으면 기본값 0.78)
            adaptive_threshold = float(threshold_row[0]) if threshold_row else 0.78

            if not rows:
                return {
                    "response": "⚠️ VOICE LOCK이 활성화되어 있으나, 등록된 목소리 성문이 없습니다. 시스템 Status 패널에서 목소리를 먼저 등록해 주세요."
                }

            if not voice_file:
                return {
                    "response": "⚠️ VOICE LOCK이 활성화되어 있으나, 명령 음성 데이터가 전달되지 않았습니다."
                }

            # 2. 전달받은 음성의 80차원 특징 벡터 추출
            audio_bytes = await voice_file.read()
            test_features = extract_mfcc_from_bytes(audio_bytes)
            if test_features is None:
                return {
                    "response": "⚠️ 오디오 데이터 파싱에 실패했습니다. (16-bit PCM WAV 형식이 맞는지 확인해 주세요)"
                }

            # 3. 모든 등록된 성문과의 앙상블 유사도 계산 → 최댓값 산출
            similarities = []
            for row in rows:
                stored_features = np.array(json.loads(row[0]))
                # 벡터 차원 불일치 시 (구 버전 13차원) 재등록 안내
                if len(stored_features) != len(test_features):
                    return {
                        "response": "⚠️ [Voice Lock] 성문 데이터 버전이 맞지 않습니다. 목소리를 다시 등록해 주세요. (새 80차원 버전)"
                    }
                sim = calculate_voice_similarity(stored_features, test_features)
                similarities.append(sim)
                print(f"[Voice Lock] Ensemble similarity with '{row[1]}': {sim*100:.2f}%")

            max_similarity = max(similarities) if similarities else 0.0
            print(f"[Voice Lock] Max similarity: {max_similarity*100:.2f}% | Threshold: {adaptive_threshold*100:.1f}%")

            # 적응형 임계값으로 판단
            if max_similarity < adaptive_threshold:
                return {
                    "response": f"⚠️ [VOICE LOCK 거부] 등록되지 않은 목소리입니다. (유사도: {max_similarity*100:.1f}% / 기준: {adaptive_threshold*100:.1f}%)"
                }

        except Exception as e:
            return {"response": f"자비스 목소리 잠금 확인 중 오류 발생: {str(e)}"}
            
    # 검증 통과 시 혹은 Lock 비활성화 시 대화 실행
    return execute_ai_chat(message)


# ─────────────────────────────────────────────────────────────────
# 출장 상담 노트 기능
# ─────────────────────────────────────────────────────────────────

def save_consultation_note(client_name: str, raw_note: str) -> str:
    """상담 일지 내용을 구조화하여 구글 문서(Google Docs)에 기록합니다.

    Args:
        client_name: 상담을 진행한 내담자의 이름
        raw_note: 상담 대화나 호소의 가공되지 않은 날것의 상담 원본 내용
    """
    from datetime import datetime

    today_str = datetime.now().strftime("%Y년 %m월 %d일")
    now_time = datetime.now().strftime("%H:%M")
    doc_title = f"상담 일지 - {today_str}"

    # ── 1. Gemini로 상담 내용 구조화 ──
    try:
        structure_model = genai.GenerativeModel("gemini-2.0-flash")
        structure_prompt = f"""다음 상담 원본 내용을 아래 형식으로 정리해 주세요.
내담자 이름: {client_name}
원본 내용: {raw_note}

출력 형식 (마크다운 없이 순수 텍스트로):
주요 호소: [내담자가 호소하는 핵심 문제 1~2줄]
상담 내용: [상담에서 다룬 주요 내용 2~4줄]
다음 과제: [내담자에게 제안한 다음 과제 또는 숙제, 없으면 '없음']
"""
        structure_response = structure_model.generate_content(structure_prompt)
        structured_text = structure_response.text.strip()
    except Exception as e:
        structured_text = f"주요 호소: (구조화 실패)\n상담 내용: {raw_note}\n다음 과제: 없음"
        print(f"[Consultation] Gemini structuring failed: {e}")

    # ── 2. 자격증명 로드 ──
    try:
        if not os.path.exists(TOKEN_FILE):
            return "⚠️ Google 인증이 필요합니다. 브라우저에서 로그인해 주세요."
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
    except Exception as e:
        return f"⚠️ Google 인증 오류: {str(e)}"

    # ── 3. 오늘 날짜 Docs 문서 검색 (Drive에서) ──
    doc_id = None
    try:
        drive_service = build("drive", "v3", credentials=creds)
        query = f"name='{doc_title}' and mimeType='application/vnd.google-apps.document' and trashed=false"
        results = drive_service.files().list(q=query, fields="files(id, name)", pageSize=1).execute()
        files = results.get("files", [])
        if files:
            doc_id = files[0]["id"]
            print(f"[Consultation] Found existing doc: {doc_id}")
    except Exception as e:
        print(f"[Consultation] Drive search failed: {e}")

    docs_service = build("docs", "v1", credentials=creds)

    # ── 4. 문서 생성 또는 기존 문서에 추가 ──
    try:
        if doc_id is None:
            # 새 문서 생성
            doc = docs_service.documents().create(body={"title": doc_title}).execute()
            doc_id = doc["documentId"]
            # 헤더 삽입
            header_text = f"상담 일지 - {today_str}\n{'─' * 40}\n"
            docs_service.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"insertText": {"location": {"index": 1}, "text": header_text}}]}
            ).execute()
            print(f"[Consultation] Created new doc: {doc_id}")

        # 현재 문서 끝 위치 파악
        doc_content = docs_service.documents().get(documentId=doc_id).execute()
        end_index = doc_content["body"]["content"][-1]["endIndex"] - 1

        # 추가할 상담 노트 텍스트
        note_block = (
            f"\n[내담자] {client_name}  |  {now_time}\n"
            f"{'─' * 40}\n"
            f"{structured_text}\n"
        )

        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertText": {"location": {"index": end_index}, "text": note_block}}]}
        ).execute()

    except Exception as e:
        return f"⚠️ Google Docs 저장 실패: {str(e)}"

    # ── 5. 로컬 DB에도 인덱스 저장 ──
    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    try:
        conn = sqlite3.connect("jarvis_memory.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO consultations (client_name, structured_note, docs_url) VALUES (?, ?, ?)",
            (client_name, structured_text, doc_url)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Consultation] Local DB save failed: {e}")

    return f"✅ {client_name} 내담자 상담 노트가 Google Docs에 저장되었습니다.\n📄 문서 바로가기: {doc_url}"




class TaskOrchestrator:
    def __init__(self):
        # 시나리오별 단계 및 권한 등급(Permission Tier) 정의
        self.scenario_definitions = {
            "상담 준비": [
                {"name": "구글 드라이브 문서 검색", "tier": 1},
                {"name": "문서 상세 내용 읽기", "tier": 1},
                {"name": "AI 분석 요약 및 아웃라인 생성", "tier": 1},
                {"name": "구글 캘린더 일정 등록", "tier": 2},
                {"name": "준비 내용 결과 메일 발송", "tier": 2}
            ],
            "회의 준비": [
                {"name": "구글 드라이브 회의 관련 문서 검색", "tier": 1},
                {"name": "문서 상세 내용 읽기", "tier": 1},
                {"name": "AI 회의 안건 분석 및 요약 브리프 생성", "tier": 1},
                {"name": "구글 캘린더 회의 세션 등록", "tier": 2},
                {"name": "회의 참가자 메일 발송", "tier": 2}
            ]
        }

    def run_scenario(self, scenario_name: str, keyword: str, **kwargs) -> str:
        if scenario_name not in self.scenario_definitions:
            return f"오류: 지원하지 않는 시나리오입니다. (가능한 시나리오: {', '.join(self.scenario_definitions.keys())})"
        
        steps = self.scenario_definitions[scenario_name]
        
        # 1. 분류 엔진(Classifier) 구동
        has_tier2 = any(step["tier"] == 2 for step in steps)
        
        # Level 2 작업이 있는 경우 즉시 실행하지 않고 승인을 대기
        if has_tier2:
            # 대기 상태 데이터를 JSON 직렬화하여 DB에 임시 저장 (user_profiles 테이블)
            import json
            pending_data = {
                "scenario_name": scenario_name,
                "keyword": keyword,
                "kwargs": kwargs
            }
            save_user_profile("pending_scenario", json.dumps(pending_data))
            
            # 사용자에게 단계 분류 목록을 보여주며 승인 요청 구성
            response_lines = [
                f"요청하신 '{scenario_name}' 시나리오를 분석했습니다.",
                "이 시나리오는 다음과 같은 작업들로 구성되어 있어 사용자의 명시적 승인이 필요합니다.\n",
                "[작업 목록 및 권한 등급]"
            ]
            for step in steps:
                tier_label = "[Level 1: 자동 실행 가능]" if step["tier"] == 1 else "[Level 2: 승인 필요]"
                response_lines.append(f"- {step['name']} ({tier_label})")
                
            response_lines.append("\n위 작업을 순차적으로 실행하시겠습니까? 진행하려면 '승인' 또는 '진행해'라고 말씀해 주세요.")
            return "\n".join(response_lines)
            
        else:
            # Level 1 작업들만 있는 경우 즉시 실행
            return self.run_scenario_final(scenario_name, keyword, **kwargs)

    def run_scenario_final(self, scenario_name: str, keyword: str, **kwargs) -> str:
        """승인이 완료되었거나 Level 1 작업들만 존재할 때 실제 비즈니스 파이프라인 일괄 실행"""
        if scenario_name == "상담 준비":
            return self._execute_consultation_prep(keyword, **kwargs)
        elif scenario_name == "회의 준비":
            return self._execute_meeting_prep(keyword, **kwargs)
        return "오류: 알 수 없는 시나리오입니다."

    def _execute_consultation_prep(self, keyword: str, **kwargs) -> str:
        steps_log = []
        # Level 1 작업은 바로 수행
        steps_log.append("1단계 [Level 1]: 구글 드라이브에서 내담자 관련 문서 검색 중...")
        try:
            search_res = search_drive_files(keyword)
            steps_log.append(f"검색 결과:\n{search_res}\n")
        except Exception as e_search:
            search_res = ""
            steps_log.append(f"검색 오류: {e_search}\n")
        
        import re
        file_id = None
        if search_res:
            file_ids = re.findall(r"/d/([a-zA-Z0-9-_]+)", search_res)
            file_id = file_ids[0] if file_ids else None
        
        file_content = ""
        if file_id:
            steps_log.append(f"2단계 [Level 1]: 문서(ID: {file_id}) 상세 내용 파싱 중...")
            try:
                file_content = read_drive_file_content(file_id)
                steps_log.append("문서 내용 획득 성공.\n")
            except Exception as e_read:
                file_content = f"기존 기록 파싱 실패: {e_read}"
                steps_log.append(f"문서 파싱 오류: {e_read}\n")
        else:
            steps_log.append("2단계 건너뜀 [Level 1]: 관련 문서를 드라이브에서 찾지 못했습니다.\n")
            file_content = f"기존 기록 없음. {keyword} 님과의 신규 상담 준비."

        steps_log.append("3단계 [Level 1]: 제미나이 AI를 활용하여 상담 요약 및 아웃라인 분석 중...")
        summary_text = ""
        try:
            summary_model = genai.GenerativeModel("models/gemini-2.5-flash")
            summary_prompt = f"다음 문서를 바탕으로 상담을 준비하기 위한 핵심 요약과 아웃라인을 격식 있는 한국어로 정리해줘. 만약 문서 내용이 부실하다면 상담 준비 가이드를 제공해줘.\n내용:\n{file_content}"
            summary_response = summary_model.generate_content(summary_prompt)
            summary_text = summary_response.text.strip()
            steps_log.append(f"분석 결과:\n{summary_text}\n")
        except Exception as e_sum:
            summary_text = f"상담 대상: {keyword}님 준비 자료."
            steps_log.append(f"분석 오류로 기본 요약 대체: {e_sum}\n")

        # Level 2 작업 수행
        steps_log.append("4단계 [Level 2]: 구글 캘린더에 상담 준비 세션 등록 중...")
        try:
            from datetime import datetime, timedelta
            start_time = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:00+09:00")
            end_time = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:00+09:00")
            cal_title = f"{keyword} 님과의 상담 세션"
            cal_res = Calendar(title=cal_title, start_time=start_time, end_time=end_time)
            steps_log.append(f"캘린더 등록 결과: {cal_res}\n")
        except Exception as e_cal:
            steps_log.append(f"캘린더 등록 오류: {e_cal}\n")

        steps_log.append("5단계 [Level 2]: 참석자/본인에게 준비 내용 메일 발송 중...")
        try:
            profiles = get_all_user_profiles()
            user_email = profiles.get("user_email", "owner@example.com")
            creds = get_credentials()
            if creds:
                try:
                    service = build("oauth2", "v2", credentials=creds)
                    user_info = service.userinfo().get().execute()
                    if user_info.get("email"):
                        user_email = user_info.get("email")
                except Exception:
                    pass
            
            email_body = f"안녕하세요, 자비스입니다.\n요청하신 '{keyword}' 상담 준비 시나리오 분석 결과 보고서입니다.\n\n[상담 아웃라인]\n{summary_text}\n\n[캘린더 일정]\n시작: {start_time}\n종료: {end_time}\n\n감사합니다."
            email_res = send_email(to=user_email, subject=f"[JARVIS] {keyword} 상담 준비 세션 아웃라인", body=email_body)
            steps_log.append(f"이메일 발송 결과: {email_res}\n")
        except Exception as e_mail:
            steps_log.append(f"이메일 발송 오류: {e_mail}\n")

        steps_log.append("요청하신 정보를 조회하여 정리했습니다.")
        return "\n".join(steps_log)

    def _execute_meeting_prep(self, keyword: str, **kwargs) -> str:
        steps_log = []
        # Level 1 작업은 바로 수행
        steps_log.append("1단계 [Level 1]: 구글 드라이브에서 회의 관련 파일 검색 중...")
        try:
            search_res = search_drive_files(keyword)
            steps_log.append(f"검색 결과:\n{search_res}\n")
        except Exception as e_search:
            search_res = ""
            steps_log.append(f"검색 오류: {e_search}\n")
        
        import re
        file_id = None
        if search_res:
            file_ids = re.findall(r"/d/([a-zA-Z0-9-_]+)", search_res)
            file_id = file_ids[0] if file_ids else None
        
        file_content = ""
        if file_id:
            steps_log.append(f"2단계 [Level 1]: 파일(ID: {file_id}) 상세 내용 파싱 중...")
            try:
                file_content = read_drive_file_content(file_id)
                steps_log.append("회의 파일 내용 획득 성공.\n")
            except Exception as e_read:
                file_content = f"기존 기록 파싱 실패: {e_read}"
                steps_log.append(f"문서 파싱 오류: {e_read}\n")
        else:
            steps_log.append("2단계 건너뜀 [Level 1]: 관련 문서를 드라이브에서 찾지 못했습니다.\n")
            file_content = f"기존 기록 없음. {keyword} 관련 신규 회의 준비."

        steps_log.append("3단계 [Level 1]: 제미나이 AI를 활용하여 회의 안건 분석 및 브리프 작성 중...")
        summary_text = ""
        try:
            summary_model = genai.GenerativeModel("models/gemini-2.5-flash")
            summary_prompt = f"다음 회의 자료를 바탕으로 회의 참석자들을 위한 핵심 요약 브리프와 논의 아젠다를 깔끔하게 정리해줘. 자료가 부족하면 가이드라인을 제안해줘.\n내용:\n{file_content}"
            summary_response = summary_model.generate_content(summary_prompt)
            summary_text = summary_response.text.strip()
            steps_log.append(f"분석 결과:\n{summary_text}\n")
        except Exception as e_sum:
            summary_text = f"회의 주제: {keyword} 관련 준비 자료."
            steps_log.append(f"분석 오류로 기본 요약 대체: {e_sum}\n")

        # Level 2 작업 수행
        steps_log.append("4단계 [Level 2]: 구글 캘린더에 회의 세션 등록 중...")
        try:
            from datetime import datetime, timedelta
            start_time = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:00+09:00")
            end_time = (datetime.now() + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:00+09:00")
            cal_title = f"{keyword} 회의 세션"
            cal_res = Calendar(title=cal_title, start_time=start_time, end_time=end_time)
            steps_log.append(f"캘린더 등록 결과: {cal_res}\n")
        except Exception as e_cal:
            steps_log.append(f"캘린더 등록 오류: {e_cal}\n")

        steps_log.append("5단계 [Level 2]: 회의 참가자/본인에게 아젠다 메일 발송 중...")
        try:
            profiles = get_all_user_profiles()
            user_email = profiles.get("user_email", "owner@example.com")
            creds = get_credentials()
            if creds:
                try:
                    service = build("oauth2", "v2", credentials=creds)
                    user_info = service.userinfo().get().execute()
                    if user_info.get("email"):
                        user_email = user_info.get("email")
                except Exception:
                    pass
            
            email_body = f"안녕하세요, 자비스입니다.\n요청하신 '{keyword}' 회의 준비 시나리오 분석 결과 보고서입니다.\n\n[회의 요약 및 아젠다]\n{summary_text}\n\n[회의 일정]\n시작: {start_time}\n종료: {end_time}\n\n감사합니다."
            email_res = send_email(to=user_email, subject=f"[JARVIS] {keyword} 회의 준비 세션 브리프", body=email_body)
            steps_log.append(f"이메일 발송 결과: {email_res}\n")
        except Exception as e_mail:
            steps_log.append(f"이메일 발송 오류: {e_mail}\n")

        steps_log.append("요청하신 정보를 조회하여 정리했습니다.")
        return "\n".join(steps_log)


def run_orchestrated_scenario(scenario_name: str, keyword: str) -> str:
    """사용자가 고수준의 복합 작업 패키지인 '상담 준비' 또는 '회의 준비' 시나리오를 지시하면,
    사전에 정의된 도구 파이프라인(검색 -> 본문 리딩 -> AI 요약 분석 -> 캘린더 등록 -> 메일 전송)을 자동으로 실행하는 시나리오 오케스트레이터입니다.
    
    Args:
        scenario_name: 실행할 시나리오 이름 ('상담 준비' 또는 '회의 준비')
        keyword: 검색 대상이자 시나리오의 핵심 타겟 키워드 (예: '김철수', '프로젝트 알파')
    """
    orchestrator = TaskOrchestrator()
    return orchestrator.run_scenario(scenario_name, keyword)


# Gemini Generative Model 설정 (모든 조회, 행동 및 감각 도구들 등록 및 시스템 지침 강화)
if GEMINI_API_KEY:
    gemini_model = genai.GenerativeModel(
        model_name="models/gemini-2.5-flash",
        tools=[
            check_schedule_in_range, check_unread_emails, Calendar, send_email, run_orchestrated_scenario,
            search_drive_files, manage_tasks, append_sheet_data, delete_calendar_event,
            clear_chat_history, get_current_time, save_user_profile, delete_user_profile,
            read_drive_file_content, make_http_request, get_weather, save_consultation_note,
            mark_email_as_read
        ],
        system_instruction="너는 사용자의 개인 비서 자비스(JARVIS)야. 너는 사용자에게 전달받은 함수(Tool)의 결과값을 단순 나열하지 않고, 지능적으로 분석하고 요약할 수 있는 능력이 있어. 메일 데이터를 받으면 본문 내용(Snippet)을 파악하여 중요도를 판단하고 친절하게 요약해 줘. 이제 너는 일정을 읽는 것뿐만 아니라 직접 캘린더에 일정을 생성하고, 직접 이메일을 발송할 수 있는 완벽한 비서야. 너는 이제 구글 드라이브 문서 검색, To-Do(할 일) 리스트 관리, 그리고 구글 스프레드시트에 데이터를 직접 기록할 수 있는 완벽한 전천후 비서야. 너는 이제 일정을 삭제하고 대화 기록을 초기화할 수 있는 관리 권한을 가졌어. 하지만 데이터 삭제는 위험한 작업이므로, 사용자가 삭제를 명확히 요청했을 때만 수행해. 삭제 도구를 호출하기 전, 만약 필요한 경우 사용자에게 한 번 더 확인할 수도 있어. 사용자가 일정을 등록, 추가, 생성해달라고 지시하면, 너의 임의 상상으로 등록이 완료되었다고 응답하지 말고, 반드시 Calendar 툴을 호출해 성공 결과(일정 링크 포함)를 직접 리턴받은 후 이를 바탕으로 사용자에게 답변하라. 또한, 사용자가 특정 일정이나 회의를 삭제해 달라고 요청하면, 먼저 check_schedule_in_range 등의 도구를 호출하여 삭제하고자 하는 일정의 고유 ID(event_id)를 찾은 뒤, delete_calendar_event 도구를 그 ID로 호출하여 삭제를 완료하라. 여러 일정을 지우고자 하거나 중복된 일정이 있다면, 각각의 ID를 확인하여 delete_calendar_event를 필요한 만큼 여러 번 호출하여 한 번에 지워라. 절대 사용자에게 고유 ID값을 직접 물어보지 말고, 도구 조회를 통해 스스로 알아내어 삭제하라. 사용자가 오늘 날짜, 현재 시각, 현재 요일 등을 질문하거나, 특정 날짜(오늘, 내일, 모레, 어제, 이번주 등) 기준의 일정 연산/조회/생성/삭제가 필요할 때는 반드시 get_current_time 도구를 가장 먼저 호출하여 기준 시간을 획득한 뒤 행동하라. 절대로 임의의 날짜를 계산하거나 시간 정보를 모른다고 거절하지 마라. 예를 들어 내일 일정을 묻는 경우, get_current_time을 호출하여 오늘 날짜가 2026-06-17임을 파악한 뒤, 내일 날짜인 2026-06-18을 계산하여 check_schedule_in_range(start_date='2026-06-18', end_date='2026-06-18')을 호출해야 한다. 사용자가 날씨 정보를 요청하거나 조회를 원할 때는, 절대 직접 정보를 불러올 수 없다고 답하지 말고 반드시 get_weather 도구를 호출하여 오늘, 내일 또는 3일간의 예보 등 필요한 날씨 데이터를 획득한 후 답변을 가공해 설명하라. 뉴스 등 기타 외부의 실시간 정보를 원할 때는 make_http_request 도구로 외부 공개 API를 요청하여 데이터를 획득하라. 사용자가 구글 드라이브의 문서 상세 본문 내용을 읽거나 분석/요약해달라고 요청하면, 먼저 search_drive_files로 파일을 찾은 뒤 해당 파일의 ID로 read_drive_file_content 도구를 호출하여 파일 내용을 읽어와 분석하고 요약하라. 사용자가 상담 내용을 기록해달라거나, 내담자 정보를 저장해달라거나, 상담 노트를 남겨달라고 요청하면 save_consultation_note 도구를 호출하라. 사용자가 자신의 개인 정보(이름, 호칭, 취향, 거주지, 관심사, 선호도, 규칙 등)를 설명하거나 변경을 요청하면, 답변에만 머무르지 말고 반드시 save_user_profile 도구를 호출하여 해당 정보를 기억장치(DB)에 기록해 두라. 키값(key)은 정보의 의미를 나타내는 간결한 영어(예: user_name, favorite_drink, birthday)로 설정하고, 사용자가 물어볼 때 저장된 정보를 불러와 적절히 활용하라. 또한, 사용자가 '상담 준비', '회의 준비'와 같이 여러 도구들의 실행이 순차적으로 수반되는 고수준 복합 시나리오 작업을 요청하면, 개별 도구들을 일일이 호출하는 대신 반드시 run_orchestrated_scenario 도구를 scenario_name 및 keyword(예: '상담 준비', '김철수') 인자로 호출하여 시나리오 오케스트레이션을 실행시켜야 한다. 또한, 사용자가 메일(이메일)을 읽음 처리해달라고 하거나, 방금 온 메일이나 특정 메일을 다 읽었다고 하면 반드시 mark_email_as_read 도구를 호출하여 해당 메일을 읽음 처리해 주어야 한다. 메일 목록 조회를 통해 ID를 획득하여 호출해라. 또한, 너는 음성 비서이므로 사용자와 대화할 때 텍스트 답변이 구어체(말하는 말투)로 자연스럽고 매끄럽게 작성되도록 해줘. 글머리 기호(마크다운), 대괄호, 코드 블록, 특수 기호는 완전히 피하고, 실제 사람이 귀에 대고 말하듯이 부드럽고 격식 있는 문장으로 대답해라."
    )
else:
    gemini_model = None


class ConsultationRequest(BaseModel):
    client_name: str
    raw_note: str


@app.post("/api/consultation")
def create_consultation(payload: ConsultationRequest):
    """상담 노트를 Gemini로 구조화하고 Google Docs에 저장합니다."""
    result = save_consultation_note(payload.client_name, payload.raw_note)
    if result.startswith("⚠️"):
        raise HTTPException(status_code=500, detail=result)
    return {"message": result}


@app.get("/api/consultation/today")
def get_today_consultations():
    """오늘 저장된 상담 노트 목록을 반환합니다."""
    from datetime import datetime
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect("jarvis_memory.db")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT client_name, structured_note, docs_url, timestamp FROM consultations WHERE date(timestamp) = ? ORDER BY id ASC",
            (today,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [
            {
                "client_name": r[0],
                "structured_note": r[1],
                "docs_url": r[2],
                "time": r[3][11:16] if r[3] else ""
            }
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


from fastapi.responses import FileResponse

@app.get("/manifest.json")
def get_manifest():
    return FileResponse("manifest.json", media_type="application/manifest+json")

@app.get("/sw.js")
def get_service_worker():
    return FileResponse("sw.js", media_type="application/javascript")


@app.delete("/ai/chat/history")
def clear_chat_history_endpoint():
    """DB에 저장된 대화 기억(메시지 기록)을 모두 삭제합니다."""
    res = clear_chat_history()
    if "오류" in res:
        raise HTTPException(status_code=500, detail=res)
    return {"message": "대화 기억이 성공적으로 초기화되었습니다."}


@app.post("/api/gmail/read/{message_id}")
def mark_email_as_read_endpoint(message_id: str):
    """이메일을 읽음 처리하는 엔드포인트"""
    res = mark_email_as_read(message_id)
    if "오류" in res or "않습니다" in res:
        raise HTTPException(status_code=500, detail=res)
    return {"message": res}


@app.get("/api/devices")
def get_devices():
    """스위치봇, 스마트플러그 등 등록된 모든 IoT 스마트 기기 상태 조회"""
    profiles = get_all_user_profiles()
    switchbot = profiles.get("device_switchbot", "OFF")
    smartplug = profiles.get("device_smartplug", "OFF")
    return {"switchbot": switchbot, "smartplug": smartplug}


@app.post("/api/devices/{device_id}/toggle")
def toggle_device(device_id: str):
    """지정된 IoT 스마트 기기 상태 토글 (ON <-> OFF)"""
    if device_id not in ["switchbot", "smartplug"]:
        raise HTTPException(status_code=400, detail="지원하지 않는 기기 ID입니다.")
    profiles = get_all_user_profiles()
    current_state = profiles.get(f"device_{device_id}", "OFF")
    new_state = "ON" if current_state == "OFF" else "OFF"
    save_user_profile(f"device_{device_id}", new_state)
    return {device_id: new_state}


@app.get("/api/briefing")
def daily_briefing(force: bool = False):
    """아침 데일리 브리핑 데이터를 생성하고 오디오와 텍스트를 반환합니다.
    force가 False이면 오늘 이미 브리핑을 받았는지 여부를 확인하여 오늘 최초 접속 시에만 실행됩니다.
    """
    creds = get_credentials()
    if not creds:
        # 로그인되지 않은 경우 브리핑 불가능
        return {"response": None, "audio": None, "triggered": False}
        
    if not force and check_briefing_triggered_today():
        return {"response": None, "audio": None, "triggered": False}
        
    try:
        briefing_text = generate_daily_briefing_text()
        # 브리핑을 성공적으로 생성했으므로 오늘 받았음을 기록
        record_briefing_triggered_today()
        
        # 대화 기록에도 저장하여 대화가 끊기지 않게 함
        save_chat_message("model", briefing_text)
        
        audio_content = synthesize_speech_elevenlabs(get_tts_text(briefing_text))
        if not audio_content:
            audio_content = synthesize_speech(get_tts_text(briefing_text))
            
        return {
            "response": briefing_text,
            "audio": audio_content,
            "triggered": True
        }
    except Exception as e:
        print("Daily Briefing Generation Error:", e)
        return {"response": f"아침 브리핑 생성 중 오류가 발생했습니다: {str(e)}", "audio": None, "triggered": True}


# background_monitor_loop는 상단에 정의된 완벽한 버전(Gmail + Calendar + Hotspot + Smartplug 감시가 모두 포함됨)을 실행합니다.


def start_background_monitoring():
    import threading
    thread = threading.Thread(target=background_monitor_loop, daemon=True)
    thread.start()


@app.on_event("startup")
def on_startup():
    start_background_monitoring()


@app.get("/api/notifications")
def get_notifications():
    """읽지 않은 자비스 알림(선제적 알림) 목록을 가져오고 읽음 처리합니다."""
    try:
        conn = sqlite3.connect("jarvis_memory.db")
        cursor = conn.cursor()
        # 읽지 않은 알림 가져오기
        cursor.execute("SELECT id, title, message, timestamp FROM notifications WHERE read = 0 ORDER BY id ASC")
        rows = cursor.fetchall()
        
        # 가져온 알림들은 읽음 처리
        if rows:
            ids = [row[0] for row in rows]
            placeholders = ",".join(["?"] * len(ids))
            cursor.execute(f"UPDATE notifications SET read = 1 WHERE id IN ({placeholders})", ids)
            conn.commit()
            
        conn.close()
        
        return [{"id": r[0], "title": r[1], "message": r[2], "timestamp": r[3]} for r in rows]
    except Exception as e:
        print("Error fetching notifications:", e)
        return []


