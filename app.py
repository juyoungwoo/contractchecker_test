# app.py
# -*- coding: utf-8 -*-
"""
Streamlit: 계약서 자동 이슈 마킹(독소조항) 리뷰어 — Google Sheet(비공개, Secrets) 기반
"""

from __future__ import annotations
import os, io, re, json, time, uuid, html, unicodedata
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from collections import defaultdict

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

# ---------------- Clause splitter (핵심 수정) ----------------
def split_into_clauses_kokr(text: str) -> List[Clause]:
    """
    '제 O조 (조항명)' 패턴을 기준으로 계약서를 분할합니다.
    각 조의 제목(괄호 포함)을 명확하게 인식하고, 그 다음 조항 시작 전까지를 본문으로 묶습니다.
    """
    # "제 <숫자> 조 (<조항명>)" 패턴으로 계약서 조항의 시작점을 찾는다.
    # 그룹 1: 조항 번호 (숫자)
    # 그룹 2: 조항 제목 (괄호 안의 내용)
    clause_pattern = re.compile(r"제\s*(\d+)\s*조\s*\(([^)]+)\)")
    matches = list(clause_pattern.finditer(text))

    if not matches:
        return []

    clauses = []
    for i, match in enumerate(matches):
        start_pos = match.start()
        # 다음 조항의 시작점을 현재 조항의 끝점으로 설정
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        
        clause_full_text = text[start_pos:end_pos].strip()
        
        # 정규식 그룹에서 조항 번호와 제목을 직접 추출
        clause_idx = int(match.group(1))
        clause_title_text = match.group(2).strip()
        
        # UI에 표시될 전체 제목을 재구성
        title = f"제{clause_idx}조 ({clause_title_text})"
        
        # 제목 부분(매치된 전체 문자열)을 제외한 나머지를 본문으로 설정
        body_only = clause_full_text[len(match.group(0)):].strip()
        
        clauses.append(Clause(idx=clause_idx, title=title, text=body_only))
            
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
            "You are a meticulous Korean legal assistant **acting on behalf of the '한국전자기술연구원' (the research institute)**. "
            "Your primary goal is to find clauses in the contract that are **disadvantageous or potentially risky for the '연구원'**. "
            "You must respond in **KOREAN**. Return a **STRICT JSON object**.\n\n"
            "📌 **CRITICAL INSTRUCTIONS:**\n"
            "1.  **'연구원'의 입장에서 분석하십시오:** `CONTRACT`를 검토하여 '연구원'에게 불리하거나 위험한 조항을 식별하십시오.\n"
            "2.  **정확한 조항 번호를 명시하십시오:** `CLAUSE_LIST`를 참고하여 제14조 2항 등으로 정확히 지정하십시오.\n"
            "3.  **문제되는 문장을 명확히 추출하십시오:** 독소 조항이 있다면 **정확한 문장 또는 구절**을 지정해야 합니다.\n"
            "4.  **위험성 설명:** 해당 문구가 왜 연구원에게 불리한지를 명확하게 설명하십시오.\n"
            "5.  **원문 인용:** 인용은 반드시 **원문 그대로의 한국어 문장**이어야 하며, 절대 의역하지 마십시오.\n"
            "6.  **간결하고 구체적으로:** 너무 긴 인용은 피하고, 한 문장 또는 한 구절처럼 간단 명확하게 하십시오.\n"
            "7.  **중복을 피하십시오:** 동일한 위험을 여러 조항에서 반복적으로 지적하지 마십시오.\n\n"
            "📌 **JSON 출력 형식 (STRICT):**\n"
            "다음 형식을 반드시 그대로 따르십시오. (하나의 JSON 객체만 반환)\n"
            "{\n"
            f"  \"issue_id\": \"{issue_id}\",\n"
            f"  \"issue_title\": \"{issue_title}\",\n"
            "  \"found\": boolean,  // true 또는 false\n"
            "  \"clause_indices\": [조 번호],  // 예: [9]\n"
            "  \"evidence_quotes\": [\"문제 문장 (원문)\"]  // 반드시 계약서 원문과 일치해야 함\n"
            "  \"explanation\": \"⚠️ 제[조번호] [항번호]항\\n[문제 문장 인용]\\n[간결한 설명 (연구원 관점)]\"\n"
            "}\n\n"
            "📌 **Explanation 필드 형식은 반드시 다음을 따르십시오:**\n"
            "- 첫 줄: ⚠️ 제[조번호] [항번호]항\\n\n"
            "- 둘째 줄: 문제 문장 그대로 인용\n\n"
            "- 셋째 줄: 왜 문제가 되는지 1~2문장으로 설명\n\n"
            "✅ 예시:\n"
            "⚠️ 제9조 2항\n"
            "본 계약에서 '연구원'은 제3자의 지식재산권 침해에 대한 모든 책임을 지게 되며, 이로 인해 발생하는 손해를 배상해야 합니다.\n\n"
            "이는 '연구원'에게 과도한 부담을 주며, 예기치 않은 법적 분쟁에 휘말릴 위험이 있습니다.\n\n"
            "🛑 이 형식을 벗어날 경우, 분석 결과가 사용자에게 표시되지 않을 수 있습니다. 반드시 지침을 따르십시오.\n"
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
    """
    인용문을 **굵게** 처리 (LLM 판단)
    """
    escaped = html.escape(text)
    for quote in quotes:
        quote = quote.strip()
        if not quote: continue
        q_escaped = html.escape(quote)
        if q_escaped in escaped:
            escaped = escaped.replace(q_escaped, f"<b>{q_escaped}</b>")
        else:
            raw_bold = f"<b>{quote}</b>"
            if quote in text:
                escaped = escaped.replace(html.escape(quote), html.escape(raw_bold))
    return escaped.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")



# ---------------- UI ----------------
st.set_page_config(page_title="계약서 독소 조항 분석기", layout="wide")
st.title("📑 계약서 독소 조항 분석기")

with st.sidebar:
    st.header("⚙️ 설정(필요시)")
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
            st.error("계약서에서 '제 O조' 형식의 조항을 찾을 수 없어 분석을 진행할 수 없습니다."); st.stop()
        
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
        st.success("✅ 검토 결과, '연구원'에게 특별히 불리한 독소 조항이 발견되지 않았습니다.")
    else:
        # --- 여기부터 수정 ---
        # 각 이슈별로 발견된 증거(quotes)의 개수를 모두 합산합니다.
        total_occurrences = sum(len(r.get("evidence_quotes", [])) for r in found_issues)
        
        # 합산된 총 '건수'를 표시합니다.
        st.error(f"🚨 총 {total_occurrences}건의 이슈가 발견되었습니다.")
        # --- 여기까지 수정 ---
    
    st.subheader("📄 검토가 필요한 조항")
    
    issue_clause_indices = sorted(list({idx for issue in found_issues for idx in issue.get("clause_indices", [])}))
    clauses_with_issues = [c for c in clauses if c.idx in issue_clause_indices]

    if not clauses_with_issues and found_issues:
        st.warning("⚠️ 발견된 이슈와 매칭되는 조항을 UI에 표시하지 못했습니다. AI가 조항 번호를 제대로 인식하지 못했을 수 있습니다.")
    else:
        for c in clauses_with_issues:
            matched_issues = [r for r in found_issues if c.idx in r.get("clause_indices", [])]
            all_quotes = [q for issue in matched_issues for q in issue.get("evidence_quotes", [])]
        
            highlighted_text = highlight_text(c.text, all_quotes)
        
            with st.container(border=True):
                # 1. 전체 조 제목
                st.markdown(f"### 📄 {html.escape(c.title)}")
        
                # 2. 전체 조 내용 (강조 포함)
                st.markdown(
                    f"<div style='white-space: pre-wrap; font-size: 1rem; line-height: 1.8'>{highlighted_text}</div>",
                    unsafe_allow_html=True
                )
        
                # 3. 각 이슈에 대해 항 구분해서 설명 출력
                if matched_issues:
                    st.markdown("---")
                    항별_이슈_그룹 = defaultdict(list)
                    
                    for issue in matched_issues:
                        # 여러 증거가 있을 수 있으므로 첫 번째 증거를 기준으로 항을 찾습니다.
                        quote = issue["evidence_quotes"][0] if issue.get("evidence_quotes") else ""
                        항_match = re.search(
                            r'제?\s*(?P<label>\d+|[가-하]|[①-⑳])\s*항|(?P<label2>\d+|[가-하]|[①-⑳])\.', 
                            quote
                        )
                        항_label = 항_match.group("label") or 항_match.group("label2") if 항_match else None
                        key = 항_label if 항_label else "기타"
                        항별_이슈_그룹[key].append(issue)
                    
                    # 각 항별로 출력
                    for 항, 이슈목록 in 항별_이슈_그룹.items():
                        if 항 == "기타":
                            st.markdown(f"### 제{c.idx}조")
                        else:
                            st.markdown(f"### 제{c.idx}조 {항}항")
                    
                        for issue in 이슈목록:
                            st.markdown(issue.get("explanation", ""))
