# app.py
# -*- coding: utf-8 -*-
"""
Streamlit: 계약서 자동 이슈 마킹(독소조항) 리뷰어 — Google Sheet(비공개, Secrets) 기반
"""

from __future__ import annotations
import os, io, re, json, time, uuid, html, unicodedata
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple

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
    header_pat = re.compile(
        r"(?im)^(\s*(제\s*\d+\s*조[^\n]*?)\s*$|\s*((?:section|article)\s*\d+[^\n]*?)\s*$|\s*(\d+(?:\.\d+)*\.?\s+[^\n]{0,80})\s*$)"
    )
    headers = [(m.start(), m.group(0).strip()) for m in header_pat.finditer(text)]
    if not headers:
        # 헤더가 없으면 chunk 분할
        chunk = max(1000, len(text)//20)
        out, pos, idx = [], 0, 1
        while pos < len(text):
            end = min(len(text), pos+chunk)
            out.append(Clause(idx, f"Clause {idx}", text[pos:end], pos, end))
            pos, idx = end, idx+1
        return out
    headers.append((len(text), "__END__"))
    out = []
    for i in range(len(headers)-1):
        start, end = headers[i][0], headers[i+1][0]
        out.append(Clause(i+1, headers[i][1], text[start:end].strip(), start, end))
    return out

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
        raise RuntimeError("서비스계정 설정 없음")

    sheet_id = st.secrets.get("GSHEET_ID", "").strip()
    ws_name  = (st.secrets.get("GSHEET_WORKSHEET", "") or "").strip()

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = Credentials.from_service_account_info(cfg, scopes=scopes)
    gc = gspread.authorize(creds)  # ✅ authorize 사용

    try:
        sh = gc.open_by_key(sheet_id)
    except Exception as e:
        import traceback
        raise RuntimeError(
            f"스프레드시트 열기 실패(open_by_key): {e}\n"
            f"→ 공유/ID/API 확인\n"
            f"client_email={cfg.get('client_email')}\n"
            f"sheet_id={sheet_id}\n\n{traceback.format_exc()}"
        )

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
        self.client = OpenAI(api_key=api_key or st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY"))

    def review(self, *, model:str, issue_id:str, issue_definition:str, full_text:str) -> Dict[str, Any]:
        payload_text = full_text[:MAX_CHARS]
        system = "You are a meticulous contract reviewer. Detect ONLY the given risk. Return STRICT JSON."
        user = f"ISSUE_DEFINITION: {issue_definition}\n\nCONTRACT:\n{payload_text}"
        resp = self.client.chat.completions.create(
            model=model,
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            response_format={"type":"json_object"},
            temperature=0,
        )
        text = (resp.choices[0].message.content or "{}")
        try: data = json.loads(text)
        except: data = {"issue_id":issue_id,"found":False,"explanation":"Invalid JSON","clause_indices":[],"evidence_quotes":[]}
        data.setdefault("issue_id", issue_id)
        return data

# ---------------- UI ----------------
st.set_page_config(page_title="계약서 이슈 마킹 뷰어", layout="wide")
st.title("📑 계약서 자동 이슈 마킹 & 하이라이트 뷰어")

with st.sidebar:
    model = st.text_input("모델 이름", value=DEFAULT_MODEL)
    api_key = st.text_input("OpenAI API Key", type="password", value=os.getenv("OPENAI_API_KEY",""))

uploaded = st.file_uploader("계약서 업로드 (PDF/DOCX/TXT/MD)", type=["pdf","docx","txt","md"])
if not uploaded: st.stop()
raw_text = load_text_from_file(uploaded)
if not raw_text.strip(): st.error("텍스트 추출 실패"); st.stop()

clauses = split_into_clauses_kokr(raw_text)

try:
    issues_cfg = load_issues_from_gsheet_private()
except Exception as e:
    st.exception(e); st.stop()

if not issues_cfg: st.error("시트에 독소조항 없음"); st.stop()

llm = OpenAILLM(api_key=api_key)
progress = st.progress(0)
results = []
for i,issue in enumerate(issues_cfg,1):
    data = llm.review(model=model, issue_id=issue.get("id",f"issue_{i}"),
                      issue_definition=issue.get("definition",""), full_text=raw_text)
    data["title"] = issue.get("title","")
    results.append(data)
    progress.progress(int(i/len(issues_cfg)*100))
progress.empty()

found = [r for r in results if r.get("found")]
left,right = st.columns([2,1])

with right:
    st.subheader("탐지 요약")
    for r in found:
        with st.expander(f"✅ {r.get('title', r['issue_id'])}"):
            st.write(r.get("explanation",""))
            st.write("Indices:", r.get("clause_indices",[]))
            for q in r.get("evidence_quotes",[]): st.markdown(f"> {q}")
    st.download_button("📥 JSON 다운로드",
        data=json.dumps(results,ensure_ascii=False,indent=2).encode(),
        file_name=f"review_{int(time.time())}.json", mime="application/json")

with left:
    st.subheader("문서 보기")
    hi = {idx for r in found for idx in r.get("clause_indices",[])}
    def render(c:Clause, hl:bool):
        bg="#fffbe6" if hl else "#f6f7f9"
        return f"<div style='padding:8px;margin:8px 0;border-radius:8px;background:{bg}'><b>{html.escape(c.title)}</b><div style='white-space:pre-wrap'>{html.escape(c.text)}</div></div>"
    st.markdown("\n".join(render(c,c.idx in hi) for c in clauses), unsafe_allow_html=True)

st.success("분석 완료")
