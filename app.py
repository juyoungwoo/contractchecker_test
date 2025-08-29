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
    header_pat = re.compile(r"(?m)^(제\s*\d+\s*조[^\n]*)")
    headers = [(m.start(), m.group(0).strip()) for m in header_pat.finditer(text)]
    if not headers: return [Clause(1, "전체 계약서", text.strip(), 0, len(text))]
    headers.append((len(text), "__END__"))
    clauses: List[Clause] = []
    for i in range(len(headers) - 1):
        start, title = headers[i]
        end = headers[i + 1][0]
        body = text[start:end].strip()
        if not body: continue
        clauses.append(Clause(i + 1, title, body, start, end))
    return clauses

# ---------------- Google Sheet loader ----------------
def _normalize(s: str) -> str: return unicodedata.normalize("NFC", (s or "").strip()).lower()

def _open_worksheet_robust(sh, target_name: Optional[str]):
    if not target_name: return sh.sheet1
    try: return sh.worksheet(target_name)
    except Exception: pass
    for ws in sh.worksheets():
        if _normalize(ws.title) == _normalize(target_name): return ws
    return sh.sheet1

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
        if not OPENAI_AVAILABLE: raise RuntimeError("openai 패키지가 없습니다.")
        self.api_key = api_key or st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not self.api_key: st.error("OpenAI API 키가 필요합니다. 사이드바에서 입력해주세요."); st.stop()
        self.client = OpenAI(api_key=self.api_key)

    def review(self, *, model:str, issue_id:str, issue_title:str, issue_definition:str, full_text:str, clauses: List[Clause]) -> Dict[str, Any]:
        payload_text = full_text[:MAX_CHARS]
        
        # 각 조항의 번호와 제목을 리스트로 만들어 프롬프트에 전달
        clause_map_str = "\n".join([f"- 조항 {c.idx}: \"{c.title}\"" for c in clauses])

        # --- ✨ [수정된 부분] 시스템 프롬프트 대폭 수정 ---
        system = (
            "You are a meticulous Korean legal assistant. Your primary goal is to find specific, problematic phrases in a contract based on a given definition of a toxic clause. "
            "You must respond in KOREAN. Return a STRICT JSON object.\n\n"
            "**CRITICAL INSTRUCTIONS:**\n"
            "1.  **Analyze the Contract:** Review the entire `CONTRACT` text provided by the user.\n"
            "2.  **Identify Clause Numbers:** Use the `CLAUSE_LIST` to determine the correct clause number (e.g., 제14조 is clause 14).\n"
            "3.  **Find Specific Evidence:** If you find a toxic clause, you MUST pinpoint the **exact problematic sentence or phrase** from the contract. This is for highlighting.\n"
            "4.  **Explain the Risk:** Clearly explain WHY that specific phrase is a problem, linking it to the `ISSUE_DEFINITION`.\n"
            "5.  **JSON OUTPUT:** Your output MUST be a single JSON object with this exact schema:\n"
            "    {\n"
            f"      \"issue_id\": \"{issue_id}\",\n"
            f"      \"issue_title\": \"{issue_title}\",\n"
            "      \"found\": boolean, // `true` if found, otherwise `false`\n"
            "      \"explanation\": \"(Provide a clear, concise, and intuitive explanation in Korean. Start with an emoji like ⚠️ or 🤔.)\",\n"
            "      \"clause_indices\": number[], // **IMPORTANT**: If `found` is true, this array CANNOT be empty. It MUST contain the number(s) of the clause(s) where the issue was found.\n"
            "      \"evidence_quotes\": string[] // **IMPORTANT**: If `found` is true, this array CANNOT be empty. It MUST contain the exact quote(s).\n"
            "    }\n"
        )
        
        user = (
            f"## ISSUE_DEFINITION (검토할 독소 조항 정의):\n{issue_definition}\n\n"
            f"## CLAUSE_LIST (계약서의 조항 목록):\n{clause_map_str}\n\n"
            f"## CONTRACT (전체 계약서 내용):\n{payload_text}"
        )
    
        try:
            resp = self.client.chat.completions.create(
                model=model, messages=[{"role":"system","content":system},{"role":"user","content":user}],
                response_format={"type":"json_object"}, temperature=0.0,
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            # --- ✨ [수정된 부분] 데이터 유효성 검사 ---
            if data.get("found") and not data.get("clause_indices"):
                st.warning(f"'{issue_title}' 이슈는 발견되었으나, 관련 조항 번호를 특정하지 못했습니다.")
                data["clause_indices"] = [] # 빈 리스트로 초기화
        except Exception as e:
            st.warning(f"'{issue_title}' 검토 중 오류 발생: {e}")
            data = {"issue_id": issue_id, "found": False, "explanation": f"LLM 호출 오류: {e}", "clause_indices": [], "evidence_quotes": []}
        
        data.setdefault("issue_id", issue_id); data.setdefault("issue_title", issue_title)
        return data

# ---------------- Highlight helper ----------------
def _normalize_for_matching(text: str) -> str:
    return re.sub(r'[\s\n\r]+', '', text).lower()

def highlight_text(text: str, quotes: List[str]) -> str:
    safe_text = html.escape(text)
    temp_text = safe_text
    
    for q in quotes:
        q = q.strip()
        if not q: continue
        
        escaped_q = html.escape(q)
        # 띄어쓰기, 줄바꿈 등을 무시하고 일치하는 부분을 찾기 위한 정규식 패턴 생성
        pattern = re.escape(escaped_q).replace(r'\ ', r'\s*').replace(r'\n', r'\s*')
        
        # 원본 텍스트에서 패턴에 일치하는 모든 부분 찾기
        matches = list(re.finditer(pattern, temp_text, re.IGNORECASE | re.DOTALL))
        
        for match in reversed(matches): # 뒤에서부터 교체해야 인덱스가 꼬이지 않음
            start, end = match.span()
            original_phrase = temp_text[start:end]
            temp_text = temp_text[:start] + f"<mark>{original_phrase}</mark>" + temp_text[end:]
            
    return temp_text

# ---------------- UI ----------------
st.set_page_config(page_title="계약서 이슈 마킹 뷰어", layout="wide")
st.title("📑 계약서 자동 이슈 마킹 & 하이라이트 뷰어")

with st.sidebar:
    st.header("⚙️ 설정")
    model = st.text_input("모델 이름", value=DEFAULT_MODEL)
    api_key = st.text_input("OpenAI API Key", type="password", help="키를 입력하면 Secrets 설정보다 우선 적용됩니다.")
    
uploaded = st.file_uploader("계약서 업로드 (PDF/DOCX/TXT/MD)", type=["pdf","docx","txt","md"])
if not uploaded: st.info("분석할 계약서 파일을 업로드해주세요."); st.stop()

raw_text = load_text_from_file(uploaded)
if not raw_text.strip(): st.error("파일에서 텍스트를 추출하지 못했습니다."); st.stop()

clauses = split_into_clauses_kokr(raw_text)

if st.button("🔍 분석 시작하기", type="primary"):
    with st.spinner('계약서를 분석 중입니다. 잠시만 기다려주세요...'):
        try:
            issues_cfg = load_issues_from_gsheet_private()
        except Exception as e:
            st.exception(e); st.stop()
        if not issues_cfg:
            st.error("Google Sheet에서 독소 조항 목록을 불러오지 못했습니다."); st.stop()

        llm = OpenAILLM(api_key=api_key)
        results = []
        for issue in issues_cfg:
            data = llm.review(
                model=model, issue_id=issue.get("id", str(uuid.uuid4())),
                issue_title=issue.get("title", "Untitled"),
                issue_definition=issue.get("definition", ""), full_text=raw_text,
                clauses=clauses # 조항 목록을 LLM에 전달
            )
            results.append(data)
        
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
    
    # --- ✨ [수정된 부분] 조항 미지정 이슈 상단에 표시 ---
    unassigned_issues = [r for r in found_issues if not r.get("clause_indices")]
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
                        for q in quotes:
                            st.markdown(f"> {q}")
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
