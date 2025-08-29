# app.py
# -*- coding: utf-8 -*-
"""
Streamlit: 계약서 자동 이슈 마킹(독소조항) 리뷰어 — Google Sheet(비공개, Secrets) 기반
"""

from __future__ import annotations
import os, io, re, json, time, uuid, html, unicodedata
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

import streamlit as st
from pypdf import PdfReader

# DOCX (optional)
try:
    from docx import Document as DocxDocument
    DOCX_AVAILABLE = True
except Exception:
    DOCX_AVAILABLE = False

# gspread + google-auth
try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSHEETS_AVAILABLE = True
except Exception:
    GSHEETS_AVAILABLE = False

# OpenAI
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False

# ---------------- Constants ----------------
MAX_CHARS = 200_000
DEFAULT_MODEL = "gpt-4o-mini"

# ---------------- Data types ----------------
@dataclass
class Clause:
    idx: int
    title: str
    text: str
    start: int
    end: int

# ---------------- File loaders ----------------
def extract_text_pdf(bio: io.BytesIO) -> str:
    reader = PdfReader(bio)
    return "\n\n".join(p.extract_text() or "" for p in reader.pages)

def extract_text_docx(bio: io.BytesIO) -> str:
    if not DOCX_AVAILABLE:
        return ""
    doc = DocxDocument(bio)
    return "\n".join(p.text for p in doc.paragraphs)

def load_text_from_file(upload) -> str:
    data = upload.read()
    name = upload.name.lower()
    if name.endswith(".pdf"):
        return extract_text_pdf(io.BytesIO(data))
    if name.endswith(".docx") and DOCX_AVAILABLE:
        return extract_text_docx(io.BytesIO(data))
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="ignore")

# ---------------- Clause splitter ----------------
def split_into_clauses_kokr(text: str) -> List[Clause]:
    """
    한국 계약서를 '제 n 조' 단위로 분할.
    본문 내 '제n조' 참조는 무시하고, 줄 시작(^)에서만 매칭.
    """
    # 줄 시작에서만 '제 n 조' 잡기 (예: 제 1 조, 제1조, 제12조)
    header_pat = re.compile(r"(?m)^(제\s*\d+\s*조[^\n]*)")

    headers = [(m.start(), m.group(0).strip()) for m in header_pat.finditer(text)]
    if not headers:
        # fallback: 통째로 반환
        return [Clause(1, "전체", text.strip(), 0, len(text))]

    headers.append((len(text), "__END__"))

    clauses: List[Clause] = []
    for i in range(len(headers) - 1):
        start, title = headers[i]
        end = headers[i + 1][0]
        body = text[start:end].strip()
        clauses.append(Clause(i + 1, title, body, start, end))

    return clauses


# ---------------- Google Sheet loader ----------------
def _normalize(s: str) -> str:
    return unicodedata.normalize("NFC", (s or "").strip()).lower()

def _open_worksheet_robust(sh, target_name: Optional[str]):
    if not target_name:
        return sh.sheet1
    try:
        return sh.worksheet(target_name)
    except Exception:
        pass
    for ws in sh.worksheets():
        if _normalize(ws.title) == _normalize(target_name):
            return ws
    return sh.sheet1

def _read_secrets_gcp_sa() -> Optional[Dict[str, Any]]:
    import json as _json
    # st.secrets에 gcp_sa 키가 있는지 확인
    if "gcp_sa" in st.secrets:
        return dict(st.secrets["gcp_sa"])
    # 환경 변수 또는 st.secrets에서 GDRIVE_SERVICE_ACCOUNT_JSON 값을 읽어옴
    raw = st.secrets.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        try:
            cfg = _json.loads(raw)
            # JSON 내 private_key의 \n을 실제 개행 문자로 변경
            if "\\n" in cfg.get("private_key","") and "\n" not in cfg["private_key"]:
                cfg["private_key"] = cfg["private_key"].replace("\\n","\n")
            return cfg
        except _json.JSONDecodeError:
            st.error("GDRIVE_SERVICE_ACCOUNT_JSON의 형식이 올바르지 않습니다.")
            return None
    return None

def load_issues_from_gsheet_private() -> List[Dict[str, Any]]:
    if not GSHEETS_AVAILABLE:
        raise RuntimeError("gspread / google-auth 패키지가 필요합니다.")
    
    # GCP 서비스 계정 정보 읽기
    cfg = _read_secrets_gcp_sa()
    if not cfg:
        st.error("Streamlit Secrets에 GCP 서비스 계정 정보(gcp_sa 또는 GDRIVE_SERVICE_ACCOUNT_JSON)가 설정되지 않았습니다.")
        st.info("자세한 설정 방법은 Streamlit 문서를 참고하세요: https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management")
        return []

    # Google Sheet ID 및 워크시트 이름 읽기
    sheet_id = st.secrets.get("GSHEET_ID", "").strip()
    ws_name  = (st.secrets.get("GSHEET_WORKSHEET", "독소조항_예시")).strip() # 기본 워크시트 이름 설정

    if not sheet_id:
        st.error("Streamlit Secrets에 Google Sheet ID (GSHEET_ID)가 설정되지 않았습니다.")
        return []

    # Google API 접근 권한 설정
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    
    try:
        creds = Credentials.from_service_account_info(cfg, scopes=scopes)
        gc = gspread.authorize(creds)

        sh = gc.open_by_key(sheet_id)
        ws = _open_worksheet_robust(sh, ws_name)
        rows = ws.get_all_values()
    except gspread.exceptions.SpreadsheetNotFound:
        st.error(f"Google Sheet를 찾을 수 없습니다. ID가 올바른지 확인하세요: {sheet_id}")
        return []
    except Exception as e:
        st.error(f"Google Sheet 데이터를 불러오는 중 오류가 발생했습니다: {e}")
        return []


    issues = []
    if not rows: return issues
    # 헤더 유무를 판단하여 데이터 시작 인덱스 결정
    header = [c.strip().lower() for c in rows[0]]
    start_idx = 1 if set(["id","title","definition"]).intersection(header) else 0

    # 시트의 각 행을 읽어 독소 조항 목록 생성
    for r in rows[start_idx:]:
        if len(r) < 3: continue
        # 각 셀의 값에서 공백 제거
        a, b, c = r[0].strip(), r[1].strip(), r[2].strip()
        if not (a or b or c): continue
        issues.append({"id": a or str(uuid.uuid4()), "title": b or a or "(untitled)", "definition": c})
    return issues

# ---------------- LLM ----------------
class OpenAILLM:
    def __init__(self, api_key: Optional[str]=None):
        if not OPENAI_AVAILABLE:
            raise RuntimeError("openai 패키지가 없습니다.")
        # API 키가 없으면 사용자에게 입력 요청
        self.api_key = api_key or st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            st.error("OpenAI API 키가 필요합니다. 사이드바에서 입력해주세요.")
            st.stop()
        self.client = OpenAI(api_key=self.api_key)

    def review(self, *, model:str, issue_id:str, issue_definition:str, full_text:str) -> Dict[str, Any]:
        payload_text = full_text[:MAX_CHARS]
        system = (
            "You are a meticulous contract reviewer. "
            "Detect ONLY the given risk as defined. "
            "Return STRICT JSON with exactly this schema:\n"
            "{\n"
            "  \"issue_id\": string,\n"
            "  \"found\": boolean,\n"
            "  \"explanation\": string,\n"
            "  \"clause_indices\": number[],\n"
            "  \"evidence_quotes\": string[]\n"
            "}\n"
            "- 'clause_indices' = which clause numbers (approx) contain the issue.\n"
            "- 'evidence_quotes' = exact text snippets from the contract that triggered detection."
        )
        user = (
            f"ISSUE_DEFINITION:\n{issue_definition}\n\n"
            f"CONTRACT:\n{payload_text}"
        )
    
        try:
            resp = self.client.chat.completions.create(
                model=model,
                messages=[{"role":"system","content":system},{"role":"user","content":user}],
                response_format={"type":"json_object"},
                temperature=0,
            )
            text = (resp.choices[0].message.content or "{}")
            data = json.loads(text)
        except Exception as e:
            # API 호출 실패 또는 JSON 파싱 오류 시 기본값 반환
            st.warning(f"'{issue_id}' 검토 중 오류 발생: {e}")
            data = {
                "issue_id": issue_id,
                "found": False,
                "explanation": f"LLM 호출 오류: {e}",
                "clause_indices": [],
                "evidence_quotes": [],
            }
        
        # 기본값 설정
        data.setdefault("issue_id", issue_id)
        return data


# ---------------- Highlight helper ----------------
def highlight_text(text: str, quotes: List[str]) -> str:
    """evidence_quotes에 나온 구절을 <mark> 태그로 감싸기"""
    safe = html.escape(text)
    for q in quotes:
        q = q.strip()
        if not q:
            continue
        # 정규표현식에서 특수문자를 이스케이프 처리
        q_esc = re.escape(html.escape(q))
        safe = re.sub(q_esc, f"<mark>{html.escape(q)}</mark>", safe, flags=re.IGNORECASE)
    return safe

# ---------------- UI ----------------
st.set_page_config(page_title="계약서 이슈 마킹 뷰어", layout="wide")
st.title("📑 계약서 자동 이슈 마킹 & 하이라이트 뷰어")

# --- 사이드바 설정 ---
with st.sidebar:
    st.header("⚙️ 설정")
    # 모델 이름 입력 필드
    model = st.text_input("모델 이름", value=DEFAULT_MODEL)
    # OpenAI API 키 입력 필드 (비밀번호 타입으로)
    api_key_input = st.text_input("OpenAI API Key", type="password", help="여기에 키를 입력하면 환경변수나 Secrets 설정보다 우선 적용됩니다.")
    
    # API 키 우선순위: 1. UI 입력 2. Streamlit Secrets 3. 환경변수
    api_key = api_key_input or st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")

# --- 계약서 파일 업로드 ---
uploaded = st.file_uploader("계약서 업로드 (PDF/DOCX/TXT/MD)", type=["pdf","docx","txt","md"])
if not uploaded:
    st.info("분석할 계약서 파일을 업로드해주세요.")
    st.stop()

# 파일에서 텍스트 추출
raw_text = load_text_from_file(uploaded)
if not raw_text.strip():
    st.error("파일에서 텍스트를 추출하지 못했습니다. 파일이 비어있거나 지원하지 않는 형식일 수 있습니다.")
    st.stop()

# 텍스트를 조항별로 분리
clauses = split_into_clauses_kokr(raw_text)
st.success(f"총 {len(clauses)}개의 조항을 인식했습니다.")

# --- 분석 시작 버튼 ---
if st.button("🔍 분석 시작하기"):
    try:
        # Google Sheet에서 독소 조항 목록 불러오기
        issues_cfg = load_issues_from_gsheet_private()
    except Exception as e:
        st.exception(e)
        st.stop()

    if not issues_cfg:
        st.error("Google Sheet에서 독소 조항 목록을 불러오지 못했습니다. Secrets 설정을 확인해주세요.")
        st.stop()

    # LLM 클라이언트 초기화
    llm = OpenAILLM(api_key=api_key)
    
    # 분석 진행 상황 표시
    progress_bar = st.progress(0, text="분석을 시작합니다...")
    results = []
    total_issues = len(issues_cfg)
    
    # 각 독소 조항에 대해 LLM 리뷰 수행
    for i, issue in enumerate(issues_cfg, 1):
        progress_text = f"'{issue.get('title', '')}' 조항 검토 중... ({i}/{total_issues})"
        progress_bar.progress(int(i / total_issues * 100), text=progress_text)
        
        data = llm.review(
            model=model,
            issue_id=issue.get("id", f"issue_{i}"),
            issue_definition=issue.get("definition", ""),
            full_text=raw_text
        )
        data["title"] = issue.get("title", "")
        results.append(data)
    
    progress_bar.empty()
    st.success("분석이 완료되었습니다!")

    # --- 분석 결과 표시 ---
    found_issues = [r for r in results if r.get("found")]

    if not found_issues:
        st.info("검토 결과, 계약서에서 특별한 독소 조항이 발견되지 않았습니다.")
    else:
        st.subheader(f"🚨 총 {len(found_issues)}개의 잠재적 이슈 발견")

    st.subheader("📄 문서 보기 (본문 + 검토 의견)")

    for c in clauses:
        # 현재 조항과 관련된 이슈 필터링
        matched_issues = [r for r in results if c.idx in r.get("clause_indices", [])]
        
        # 관련된 모든 인용구(증거 문장) 수집
        all_quotes = [q for issue in matched_issues for q in issue.get("evidence_quotes", [])]
        
        # 인용구를 본문에서 하이라이트 처리
        highlighted_text = highlight_text(c.text, all_quotes)

        # UI를 2단으로 분리 (왼쪽: 계약서 내용, 오른쪽: 검토 의견)
        col1, col2 = st.columns([3, 2], gap="large")
        
        with col1:
            # 계약서 조항 표시
            st.markdown(
                f"<div style='padding: 1rem; margin: 0.5rem 0; border-radius: 8px; background-color: #f8f9fa; border: 1px solid #e9ecef;'>"
                f"<h4>{html.escape(c.title)}</h4>"
                f"<div style='white-space: pre-wrap; line-height: 1.6;'>{highlighted_text}</div>"
                f"</div>",
                unsafe_allow_html=True
            )
        
        with col2:
            if matched_issues:
                # 발견된 이슈(메모) 표시
                for issue in matched_issues:
                    st.markdown(
                        f"<div style='padding: 0.8rem; margin: 0.5rem 0; border-left: 5px solid #ff4b4b; background-color: #fff0f0; border-radius: 6px;'>"
                        f"<p style='margin: 0;'><strong>⚠️ {html.escape(issue.get('title', issue['issue_id']))}</strong></p>"
                        f"<p style='margin: 0.5rem 0 0 0;'>{html.escape(issue.get('explanation', ''))}</p>"
                        f"</div>", 
                        unsafe_allow_html=True
                    )
            else:
                # 이슈가 없는 경우, 빈 공간을 유지하여 UI 정렬 맞춤
                st.markdown("<div style='height: 1rem;'></div>", unsafe_allow_html=True)


    # --- 결과 다운로드 ---
    st.download_button(
        label="📥 JSON 결과 다운로드",
        data=json.dumps(results, ensure_ascii=False, indent=2).encode('utf-8'),
        file_name=f"review_{int(time.time())}.json",
        mime="application/json"
    )
