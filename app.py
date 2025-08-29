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

# --- 의존성 패키지 임포트 (외부 라이브러리) ---
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

# ---------------- File loaders ----------------
def extract_text_pdf(bio: io.BytesIO) -> str:
    reader = PdfReader(bio)
    return "\n\n".join(p.extract_text() or "" for p in reader.pages)

def extract_text_docx(bio: io.BytesIO) -> str:
    if not DOCX_AVAILABLE: return ""
    doc = DocxDocument(bio)
    return "\n".join(p.text for p in doc.paragraphs)

def load_text_from_file(upload) -> str:
    data = upload.read()
    name = upload.name.lower()
    if name.endswith(".pdf"): return extract_text_pdf(io.BytesIO(data))
    if name.endswith(".docx") and DOCX_AVAILABLE: return extract_text_docx(io.BytesIO(data))
    for enc in ("utf-8", "cp949", "euc-kr"):
        try: return data.decode(enc)
        except Exception: continue
    return data.decode("utf-8", errors="ignore")

# ---------------- Clause splitter ----------------
def split_into_clauses_kokr(text: str) -> List[Clause]:
    pat = re.compile(r"(제\s*\d+\s*조)")
    parts = pat.split(text)
    
    if len(parts) <= 1: return []

    clauses = []
    for i in range(1, len(parts), 2):
        delimiter = parts[i]
        content = parts[i+1].strip() if (i+1) < len(parts) else ""
        full_clause_text = (delimiter + " " + content).strip()
        title = full_clause_text.split('\n', 1)[0].strip()
        
        match = re.search(r'제\s*(\d+)\s*조', delimiter)
        if match:
            clause_idx = int(match.group(1))
            clauses.append(Clause(idx=clause_idx, title=title, text=full_clause_text))
            
    return clauses

# ---------------- Google Sheet loader ----------------
def _read_secrets_gcp_sa() -> Optional[Dict[str, Any]]:
    import json as _json
    if "gcp_sa" in st.secrets: return dict(st.secrets["gcp_sa"])
    raw = st.secrets.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        cfg = _json.loads(raw)
        if "\\n" in cfg.get("private_key",""): cfg["private_key"] = cfg["private_key"].replace("\\n","\n")
        return cfg
    return None

def load_issues_from_gsheet_private() -> List[Dict[str, Any]]:
    if not GSHEETS_AVAILABLE: raise RuntimeError("gspread / google-auth 패키지가 필요합니다.")
    cfg = _read_secrets_gcp_sa()
    if not cfg: st.error("Streamlit Secrets에 GCP 서비스 계정 정보가 설정되지 않았습니다."); return []
    sheet_id = st.secrets.get("GSHEET_ID", "").strip()
    ws_name  = (st.secrets.get("GSHEET_WORKSHEET", "독소조항_예시")).strip()
    if not sheet_id: st.error("Streamlit Secrets에 Google Sheet ID가 설정되지 않았습니다."); return []
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/drive.readonly"]
    creds = Credentials.from_service_account_info(cfg, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(ws_name)
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
        if not OPENAI_AVAILABLE: raise RuntimeError("openai 패키지가 없습니다.")
        self.api_key = api_key or st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not self.api_key: st.error("OpenAI API 키가 필요합니다. 사이드바에서 입력해주세요."); st.stop()
        self.client = OpenAI(api_key=self.api_key)

    def review(self, *, model:str, issue_id:str, issue_title:str, issue_definition:str, full_text:str, clauses: List[Clause]) -> Dict[str, Any]:
        payload_text = full_text[:MAX_CHARS]
        clause_map_str = "\n".join([f"- 조항 {c.idx}: \"{c.title}\"" for c in clauses])
        system = (
            "You are a meticulous Korean legal assistant. Your primary goal is to find specific, problematic phrases in a contract based on a given definition of a toxic clause. "
            "You must respond in KOREAN. Return a STRICT JSON object.\n\n"
            "**CRITICAL INSTRUCTIONS:**\n"
            "1.  **Analyze Contract:** Review the `CONTRACT` text.\n"
            "2.  **Identify Clause Numbers:** Use the `CLAUSE_LIST` to find the correct clause number (e.g., 제14조 is clause 14).\n"
            "3.  **Find Specific Evidence:** If you find a toxic clause, you MUST pinpoint the **exact problematic sentence or phrase**.\n"
            "4.  **Explain the Risk:** Clearly explain WHY that specific phrase is a problem.\n"
            "5.  **JSON OUTPUT:** Your output MUST be a single JSON object with this exact schema:\n"
            "    {\n"
            f"      \"issue_id\": \"{issue_id}\", \"issue_title\": \"{issue_title}\", \"found\": boolean,\n"
            "      \"explanation\": \"(Provide a clear, concise, and intuitive explanation in Korean. Start with an emoji.)\",\n"
            "      \"clause_indices\": number[], /* IMPORTANT: If `found` is true, this array MUST contain the clause number(s) and CANNOT be empty. */\n"
            "      \"evidence_quotes\": string[] /* IMPORTANT: If `found` is true, this array MUST contain the exact quote(s) and CANNOT be empty. */\n"
            "    }\n"
        )
        user = (f"## ISSUE_DEFINITION:\n{issue_definition}\n\n## CLAUSE_LIST:\n{clause_map_str}\n\n## CONTRACT:\n{payload_text}")
        try:
            resp = self.client.chat.completions.create(
                model=model, messages=[{"role":"system","content":system},{"role":"user","content":user}],
                response_format={"type":"json_object"}, temperature=0.0,
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            if data.get("found") and (not data.get("clause_indices") or not data.get("evidence_quotes")):
                data["found"] = False
        except Exception as e:
            st.warning(f"'{issue_title}' 검토 중 오류 발생: {e}")
            data = {"issue_id": issue_id, "found": False, "explanation": "LLM 호출 오류", "clause_indices": [], "evidence_quotes": []}
        data.setdefault("issue_id", issue_id); data.setdefault("issue_title", issue_title)
        return data

# ---------------- Highlight helper ----------------
def highlight_text(text: str, quotes: List[str]) -> str:
    safe_text = html.escape(text)
    for q in quotes:
        q = q.strip()
        if not q: continue
        try:
            escaped_q = html.escape(q)
            # 공백/줄바꿈에 유연하게 대처하기 위한 정규식
            pattern = r'\s*'.join(map(re.escape, list(q)))
            safe_text = re.sub(f'({pattern})', r'<mark>\1</mark>', safe_text, flags=re.IGNORECASE | re.UNICODE)
        except re.error:
            safe_text = safe_text.replace(html.escape(q), f"<mark>{html.escape(q)}</mark>")
    return safe_text

# ---------------- UI ----------------
st.set_page_config(page_title="계약서 독소 조항 분석기", layout="wide")
st.title("📑 계약서 독소 조항 분석기")

with st.sidebar:
    st.header("⚙️ 설정")
    model = st.text_input("OpenAI 모델", value=DEFAULT_MODEL)
    api_key = st.text_input("OpenAI API Key", type="password", help="키를 입력하면 Secrets 설정보다 우선 적용됩니다.")
    
uploaded = st.file_uploader("계약서 파일을 업로드하세요 (PDF/DOCX/TXT/MD)", type=["pdf","docx","txt","md"])
if not uploaded: st.info("분석할 계약서 파일을 업로드해주세요."); st.stop()

raw_text = load_text_from_file(uploaded)
if not raw_text.strip(): st.error("파일에서 텍스트를 추출하지 못했습니다."); st.stop()

clauses = split_into_clauses_kokr(raw_text)

if st.button("🔍 분석 시작하기", type="primary"):
    with st.spinner('AI가 계약서를 분석 중입니다. 잠시만 기다려주세요...'):
        if not clauses:
            st.error("계약서에서 '제 O조' 형식의 조항을 찾을 수 없어 분석을 진행할 수 없습니다.")
            st.stop()
        
        issues_cfg = load_issues_from_gsheet_private()
        if not issues_cfg:
            st.error("Google Sheet에서 분석할 독소 조항 목록을 불러오지 못했습니다."); st.stop()

        llm = OpenAILLM(api_key=api_key)
        results = [llm.review(
            model=model, issue_id=issue.get("id", str(uuid.uuid4())),
            issue_title=issue.get("title", "Untitled"),
            issue_definition=issue.get("definition", ""), full_text=raw_text,
            clauses=clauses
        ) for issue in issues_cfg]
        
        st.session_state['results'] = results
        st.success("🎉 분석이 완료되었습니다!")

if 'results' in st.session_state:
    results = st.session_state['results']
    found_issues = [r for r in results if r.get("found")]
    
    st.markdown("---")
    if not found_issues:
        st.success("✅ 검토 결과, 계약서에서 특별한 독소 조항이 발견되지 않았습니다.")
    else:
        st.error(f"🚨 총 {len(found_issues)}개의 잠재적 이슈가 발견되었습니다.")
    
    # --- ✨ [수정된 부분] 오류 코드 수정 ---
    assigned_clause_indices = {c.idx for c in clauses}
    unassigned_issues = [
        r for r in found_issues 
        if not any(idx in assigned_clause_indices for idx in r.get("clause_indices", []))
    ]

    if unassigned_issues:
        st.subheader("⚠️ 조항 미지정 이슈")
        st.warning("아래 이슈들은 계약서에서 발견되었으나, 특정 조항과 연결되지 않았습니다.")
        for issue in unassigned_issues:
            with st.container(border=True):
                 with st.chat_message("assistant", avatar="🤔"):
                    st.markdown(f"**{issue.get('issue_title')}**")
                    st.markdown(issue.get('explanation', ''))
                    quotes = issue.get("evidence_quotes", [])
                    if quotes:
                        st.markdown("**근거 문장:**")
                        for q in quotes: st.markdown(f"> {q}")
        st.markdown("---")

    st.subheader("📄 계약서 조항별 검토 결과")
    for c in clauses:
        matched_issues = [r for r in found_issues if c.idx in r.get("clause_indices", [])]
        
        all_quotes = [q for issue in matched_issues for q in issue.get("evidence_quotes", [])]
        highlighted_text = highlight_text(c.text, all_quotes)
        
        with st.container(border=True):
            st.markdown(f"### {html.escape(c.title)}")
            st.markdown(f"<div style='white-space: pre-wrap; line-height: 1.7;'>{highlighted_text}</div>", unsafe_allow_html=True)
            
            if matched_issues:
                st.markdown("---")
                for issue in matched_issues:
                    with st.chat_message("assistant", avatar="⚠️"):
                        st.markdown(f"**{issue.get('issue_title')}**")
                        st.markdown(issue.get('explanation', ''))
