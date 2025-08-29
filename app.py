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
    header_pat = re.compile(r"(?m)^(제\s*\d+\s*조[^\n]*)")

    headers = [(m.start(), m.group(0).strip()) for m in header_pat.finditer(text)]
    if not headers:
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
    if "gcp_sa" in st.secrets:
        return dict(st.secrets["gcp_sa"])
    raw = st.secrets.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        cfg = _json.loads(raw)
        if "\\n" in cfg.get("private_key","") and "\n" not in cfg["private_key"]:
            cfg["private_key"] = cfg["private_key"].replace("\\n","\n")
        return cfg
    return None

def load_issues_from_gsheet_private() -> List[Dict[str, Any]]:
    if not GSHEETS_AVAILABLE:
        raise RuntimeError("gspread / google-auth 패키지가 필요합니다.")
    cfg = _read_secrets_gcp_sa()
    if not cfg:
        st.error("Streamlit Secrets에 GCP 서비스 계정 정보(gcp_sa 또는 GDRIVE_SERVICE_ACCOUNT_JSON)가 설정되지 않았습니다.")
        return []

    sheet_id = st.secrets.get("GSHEET_ID", "").strip()
    ws_name  = (st.secrets.get("GSHEET_WORKSHEET", "독소조항_예시")).strip()

    if not sheet_id:
        st.error("Streamlit Secrets에 Google Sheet ID (GSHEET_ID)가 설정되지 않았습니다.")
        return []
        
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/drive.readonly"]
    creds = Credentials.from_service_account_info(cfg, scopes=scopes)
    gc = gspread.authorize(creds)

    sh = gc.open_by_key(sheet_id)
    ws = _open_worksheet_robust(sh, ws_name)
    rows = ws.get_all_values()

    issues = []
    if not rows: return issues
    header = [c.strip().lower() for c in rows[0]]
    start_idx = 1 if set(["id","title","definition"]).intersection(header) else 0

    for r in rows[start_idx:]:
        if len(r) < 3: continue
        a,b,c = r[0].strip(), r[1].strip(), r[2].strip()
        if not (a or b or c): continue
        issues.append({"id": a or str(uuid.uuid4()), "title": b or a or "(untitled)", "definition": c})
    return issues

# ---------------- LLM ----------------
class OpenAILLM:
    def __init__(self, api_key: Optional[str]=None):
        if not OPENAI_AVAILABLE:
            raise RuntimeError("openai 패키지가 없습니다.")
        self.api_key = api_key or st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            st.error("OpenAI API 키가 필요합니다. 사이드바에서 입력해주세요.")
            st.stop()
        self.client = OpenAI(api_key=self.api_key)

    def review(self, *, model:str, issue_id:str, issue_title:str, issue_definition:str, full_text:str) -> Dict[str, Any]:
        payload_text = full_text[:MAX_CHARS]
        
        # --- ✨ [수정된 부분] 시스템 프롬프트 수정 ---
        system = (
            "You are a helpful legal assistant who explains contract risks to non-lawyers in a simple and intuitive way. "
            "Your user is Korean, so please write all explanations in KOREAN. "
            "Detect ONLY the given risk as defined. "
            "Use emojis to make the explanation more engaging. "
            "Return a STRICT JSON object with the following schema:\n"
            "{\n"
            f"  \"issue_id\": \"{issue_id}\",\n"
            f"  \"issue_title\": \"{issue_title}\",\n"
            "  \"found\": boolean,\n"
            "  \"explanation\": \"(Provide a clear, concise, and intuitive explanation in Korean. Start with an emoji like ⚠️ or 🤔.)\",\n"
            "  \"clause_indices\": number[],\n"
            "  \"evidence_quotes\": string[]\n"
            "}\n"
            "- 'explanation': 왜 이 조항이 잠재적으로 문제가 될 수 있는지 쉽게 설명해주세요.\n"
            "- 'clause_indices': 이슈가 발견된 조항의 번호 (예: 제3조 -> 3).\n"
            "- 'evidence_quotes': 이슈를 발견한 근거가 되는 계약서의 정확한 문장."
        )
        
        user = (
            f"## 검토할 독소 조항 정의:\n{issue_definition}\n\n"
            f"## 전체 계약서 내용:\n{payload_text}"
        )
    
        try:
            resp = self.client.chat.completions.create(
                model=model,
                messages=[{"role":"system","content":system},{"role":"user","content":user}],
                response_format={"type":"json_object"},
                temperature=0.1, # 약간의 창의성을 허용하여 더 자연스러운 설명 생성
            )
            text = (resp.choices[0].message.content or "{}")
            data = json.loads(text)
        except Exception as e:
            st.warning(f"'{issue_title}' 검토 중 오류 발생: {e}")
            data = {"issue_id": issue_id, "found": False, "explanation": f"LLM 호출 오류: {e}", "clause_indices": [], "evidence_quotes": []}
        
        data.setdefault("issue_id", issue_id)
        data.setdefault("issue_title", issue_title)
        return data


# ---------------- Highlight helper ----------------
def highlight_text(text: str, quotes: List[str]) -> str:
    safe = html.escape(text)
    for q in quotes:
        q = q.strip()
        if not q: continue
        q_esc = re.escape(html.escape(q))
        safe = re.sub(q_esc, f"<mark>{html.escape(q)}</mark>", safe, flags=re.IGNORECASE)
    return safe

# ---------------- UI ----------------
st.set_page_config(page_title="계약서 이슈 마킹 뷰어", layout="wide")
st.title("📑 계약서 자동 이슈 마킹 & 하이라이트 뷰어")

with st.sidebar:
    st.header("⚙️ 설정")
    model = st.text_input("모델 이름", value=DEFAULT_MODEL)
    api_key_input = st.text_input("OpenAI API Key", type="password", help="여기에 키를 입력하면 환경변수나 Secrets 설정보다 우선 적용됩니다.")
    api_key = api_key_input or st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")

uploaded = st.file_uploader("계약서 업로드 (PDF/DOCX/TXT/MD)", type=["pdf","docx","txt","md"])
if not uploaded:
    st.info("분석할 계약서 파일을 업로드해주세요.")
    st.stop()

raw_text = load_text_from_file(uploaded)
if not raw_text.strip():
    st.error("파일에서 텍스트를 추출하지 못했습니다.")
    st.stop()

clauses = split_into_clauses_kokr(raw_text)

if st.button("🔍 분석 시작하기", type="primary"):
    try:
        issues_cfg = load_issues_from_gsheet_private()
    except Exception as e:
        st.exception(e); st.stop()

    if not issues_cfg:
        st.error("Google Sheet에서 독소 조항 목록을 불러오지 못했습니다. Secrets 설정을 확인해주세요."); st.stop()

    llm = OpenAILLM(api_key=api_key)
    progress_bar = st.progress(0, text="분석을 시작합니다...")
    results = []
    total_issues = len(issues_cfg)

    for i, issue in enumerate(issues_cfg, 1):
        progress_text = f"'{issue.get('title', '')}' 조항 검토 중... ({i}/{total_issues})"
        progress_bar.progress(int(i / total_issues * 100), text=progress_text)
        
        data = llm.review(
            model=model,
            issue_id=issue.get("id", f"issue_{i}"),
            issue_title=issue.get("title", "Untitled"),
            issue_definition=issue.get("definition", ""),
            full_text=raw_text
        )
        results.append(data)
    
    progress_bar.empty()
    st.success("🎉 분석이 완료되었습니다!")

    found_issues = [r for r in results if r.get("found")]
    st.session_state['results'] = results # 분석 결과를 세션에 저장
    st.session_state['found_issues'] = found_issues

# --- ✨ [수정된 부분] 분석 결과 표시 UI ---
if 'results' in st.session_state:
    results = st.session_state['results']
    found_issues = st.session_state['found_issues']

    if not found_issues:
        st.info("✅ 검토 결과, 계약서에서 특별한 독소 조항이 발견되지 않았습니다.")
    else:
        st.error(f"🚨 총 {len(found_issues)}개의 잠재적 이슈가 발견되었습니다.")

    st.markdown("---")
    st.subheader("📄 계약서 조항별 검토 결과")

    for c in clauses:
        matched_issues = [r for r in results if r.get("found") and c.idx in r.get("clause_indices", [])]
        all_quotes = [q for issue in matched_issues for q in issue.get("evidence_quotes", [])]
        highlighted_text = highlight_text(c.text, all_quotes)
        
        # 계약서 조항 표시
        with st.container(border=True):
            st.markdown(f"### {html.escape(c.title)}")
            st.markdown(f"<div style='white-space: pre-wrap; line-height: 1.7;'>{highlighted_text}</div>", unsafe_allow_html=True)
            
            # 발견된 이슈가 있으면 그 아래에 메모 형식으로 표시
            if matched_issues:
                st.markdown("---")
                for issue in matched_issues:
                    st.warning(f"**{issue.get('issue_title')}**")
                    st.markdown(issue.get('explanation', ''))

    # --- 결과 다운로드 ---
    st.download_button(
        label="📥 JSON 결과 다운로드",
        data=json.dumps(results, ensure_ascii=False, indent=2).encode('utf-8'),
        file_name=f"review_{int(time.time())}.json",
        mime="application/json"
    )
