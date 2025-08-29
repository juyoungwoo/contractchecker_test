# app.py
# -*- coding: utf-8 -*-
"""
Streamlit: 계약서 자동 이슈 마킹(독소조항) 리뷰어 — Google Sheet(비공개, Secrets) 기반

특징
- 독소조항 정의는 전부 **비공개 Google Sheet**에서 읽음 (링크/ID UI 노출 없음)
- 시트의 **A=id, B=title, C=definition** 컬럼을 사용
- 계약서 업로드(PDF/DOCX/TXT/MD) → LLM이 각 이슈별로 탐지 → 좌측 하이라이트/우측 요약

실행 준비
  1) requirements.txt (필수 패키지)
     streamlit\nopenai\npypdf\npython-docx\ngspread\ngoogle-auth\n
  2) Streamlit Secrets 설정
     [필수]
       - OPENAI_API_KEY: OpenAI API 키
       - GDRIVE_SERVICE_ACCOUNT_JSON: 서비스계정 JSON 원문 전체 (문자열)
       - GSHEET_ID: 스프레드시트 ID (/d/<이 값>/)
     [선택]
       - GSHEET_WORKSHEET: 워크시트 이름 (미입력 시 첫 번째 시트)

  3) Google Sheet 공유 설정
     - 해당 시트를 서비스계정 이메일에 "보기 권한"으로 공유

실행
  $ streamlit run app.py
"""
from __future__ import annotations
import os
import io
import re
import json
import time
import uuid
import html
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple

import streamlit as st
from pypdf import PdfReader

# DOCX (optional)
try:
    from docx import Document as DocxDocument  # type: ignore
    DOCX_AVAILABLE = True
except Exception:
    DOCX_AVAILABLE = False

# gspread for Google Sheets
try:
    import gspread  # type: ignore
    GSHEETS_AVAILABLE = True
except Exception:
    GSHEETS_AVAILABLE = False

# OpenAI
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False

# --------------- Constants ---------------
MAX_CHARS = 200_000            # LLM 안전 절단
DEFAULT_MODEL = "gpt-4o-mini"   # 기본 모델명 (사이드바에서 수정 가능)

# --------------- Data types ---------------
@dataclass
class Clause:
    idx: int
    title: str
    text: str
    start: int
    end: int

# --------------- File loaders ---------------
def extract_text_pdf(bio: io.BytesIO) -> str:
    reader = PdfReader(bio)
    parts = []
    for p in reader.pages:
        try:
            parts.append(p.extract_text() or "")
        except Exception:
            parts.append("")
    return "\n\n".join(parts)


def extract_text_docx(bio: io.BytesIO) -> str:
    if not DOCX_AVAILABLE:
        return ""
    doc = DocxDocument(bio)
    return "\n".join(p.text for p in doc.paragraphs)


def load_text_from_file(upload) -> str:
    name = upload.name.lower()
    data = upload.read()
    if name.endswith(".pdf"):
        return extract_text_pdf(io.BytesIO(data))
    if name.endswith(".docx") and DOCX_AVAILABLE:
        return extract_text_docx(io.BytesIO(data))
    try:
        return data.decode("utf-8")
    except Exception:
        return data.decode("cp949", errors="ignore")


# --------------- Clause splitter ---------------
def split_into_clauses_kokr(text: str) -> List[Clause]:
    """한국/영문 계약서에서 흔한 패턴으로 조항을 분할."""
    header_pat = re.compile(
        r"(?im)^(\s*(제\s*\d+\s*조[^\n]*?)\s*$|\s*((?:section|article)\s*\d+[^\n]*?)\s*$|\s*(\d+(?:\.\d+)*\.?\s+[^\n]{0,80})\s*$)"
    )
    headers: List[Tuple[int, str]] = []
    for m in header_pat.finditer(text):
        headers.append((m.start(), m.group(0).strip()))
    if not headers:
        # 헤더가 없을 경우 길이 기준으로 보수 분할
        approx = 20
        chunk = max(1000, len(text)//approx)
        pos = 0
        idx = 1
        out: List[Clause] = []
        while pos < len(text):
            end = min(len(text), pos + chunk)
            out.append(Clause(idx, f"Clause {idx}", text[pos:end], pos, end))
            pos = end
            idx += 1
        return out
    headers.append((len(text), "__END__"))
    out: List[Clause] = []
    for i in range(len(headers)-1):
        start = headers[i][0]
        end = headers[i+1][0]
        title = headers[i][1]
        body = text[start:end].strip()
        out.append(Clause(i+1, title, body, start, end))
    return out


# --------------- Google Sheet loader ---------------
# --------------- Google Sheet loader ---------------
def load_issues_from_gsheet_private() -> List[Dict[str, Any]]:
    if not GSHEETS_AVAILABLE:
        raise RuntimeError("gspread 패키지가 필요합니다. requirements.txt를 확인하세요.")

    # ✅ Secrets 읽기 (테이블 우선, 문자열 fallback)
    import json as _json
    cfg = None
    if "gcp_sa" in st.secrets:
        cfg = dict(st.secrets["gcp_sa"])  # TOML 테이블로 넣은 경우
    sa_json_str = st.secrets.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if not cfg and sa_json_str:
        cfg = _json.loads(sa_json_str)  # 예전 JSON 문자열 방식

    # ✅ private_key 줄바꿈 보정 (JSON 문자열 방식일 때 \n → 실제 개행)
    if cfg and isinstance(cfg.get("private_key"), str):
        pk = cfg["private_key"]
        if "\\n" in pk and "\n" not in pk:
            cfg["private_key"] = pk.replace("\\n", "\n")

    sheet_id = st.secrets.get("GSHEET_ID", "").strip()
    sheet_ws = st.secrets.get("GSHEET_WORKSHEET", "").strip() or None

    if not cfg or not sheet_id:
        raise RuntimeError("서비스계정/시트 설정이 없습니다. Secrets의 [gcp_sa] 또는 GDRIVE_SERVICE_ACCOUNT_JSON, 그리고 GSHEET_ID를 확인하세요.")

    # ✅ 서비스계정으로 접속
    gc = gspread.service_account_from_dict(cfg)
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(sheet_ws) if sheet_ws else sh.sheet1

    rows = ws.get_all_values()  # 2D list
    issues: List[Dict[str, Any]] = []
    if not rows:
        return issues

    header = [c.strip().lower() for c in rows[0]] if rows else []
    start_idx = 1 if set(["id", "title", "definition"]).intersection(set(header)) else 0

    for r in rows[start_idx:]:
        if not r or len(r) < 3:
            continue
        a = (r[0] or "").strip()  # id
        b = (r[1] or "").strip()  # title
        c = (r[2] or "").strip()  # definition
        if not (a or b or c):
            continue
        issues.append({
            "id": a or str(uuid.uuid4()),
            "title": b or a or "(untitled)",
            "definition": c,
        })
    return issues



# --------------- LLM provider ---------------
class OpenAILLM:
    def __init__(self, api_key: Optional[str] = None):
        if not OPENAI_AVAILABLE:
            raise RuntimeError("openai 패키지가 없습니다. 'pip install openai'")
        self.client = OpenAI(api_key=api_key or st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY"))

    def review(self, *, model: str, issue_id: str, issue_definition: str, full_text: str) -> Dict[str, Any]:
        payload_text = full_text[:MAX_CHARS]
        system = (
            "You are a meticulous contract reviewer. Your SOLE task is to detect ONE specific risk as defined."
            " Return STRICT JSON only."
        )
        user = (
            "ISSUE_DEFINITION: " + issue_definition +
            "\n\nCONTRACT (UTF-8 text):\n" + payload_text +
            "\n\nINSTRUCTIONS:\n"
            "- Look ONLY for the defined issue.\n"
            "- If found, mark found=true and include a concise explanation.\n"
            "- Identify clauses by rough index if possible (e.g., [3,7]).\n"
            "- Output STRICT JSON with this schema and NOTHING else.\n\n"
            "{\n  \"issue_id\": string,\n  \"found\": boolean,\n  \"explanation\": string,\n  \"clause_indices\": number[],\n  \"evidence_quotes\": string[]\n}"
        )
       # app.py의 OpenAILLM.review 메서드 (수정된 버전)

        resp = self.client.chat.completions.create( # <--- 수정 (1)
            model=model,
            messages=[ # <--- 수정 (2)
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        try:
            text = resp.choices[0].message.content # <--- 수정 (3)
            if not text:
                text = "{}"
        except Exception:
            text = "{}"
        try:
            data = json.loads(text)
        except Exception:
            data = {
                "issue_id": issue_id,
                "found": False,
                "explanation": "Model did not return valid JSON.",
                "clause_indices": [],
                "evidence_quotes": [],
            }
        data.setdefault("issue_id", issue_id)
        data.setdefault("found", False)
        data.setdefault("explanation", "")
        data.setdefault("clause_indices", [])
        data.setdefault("evidence_quotes", [])
        return data


# --------------- UI ---------------
st.set_page_config(page_title="계약서 이슈 마킹 뷰어", layout="wide")
st.title("📑 계약서 자동 이슈 마킹 & 하이라이트 뷰어")

with st.sidebar:
    st.header("🔧 설정")
    model = st.text_input("모델 이름", value=DEFAULT_MODEL)
    api_key = st.text_input("OpenAI API Key (선택: secrets 사용시 비워두기)", type="password", value=os.getenv("OPENAI_API_KEY", ""))
    st.caption("독소조항 정의: 비공개 Google Sheet(Secrets)에서 자동 로딩")

# 계약서 업로드
uploaded = st.file_uploader("계약서 파일 업로드 (PDF/DOCX/TXT/MD)", type=["pdf","docx","txt","md"]) 
if uploaded is None:
    st.info("계약서를 업로드하세요.")
    st.stop()

raw_text = load_text_from_file(uploaded)
if not raw_text.strip():
    st.error("문서에서 텍스트를 추출하지 못했습니다. 스캔 PDF일 수 있습니다(OCR 필요).")
    st.stop()

clauses = split_into_clauses_kokr(raw_text)

# Google Sheet에서 독소조항 불러오기
try:
    issues_cfg = load_issues_from_gsheet_private()
except Exception as e:
    st.error(f"독소조항 시트 로딩 실패: {e}")
    st.stop()

if not issues_cfg:
    st.error("시트에서 읽은 독소조항이 없습니다. A=id, B=title, C=definition을 확인하세요.")
    st.stop()

st.caption(f"불러온 이슈 정의: {len(issues_cfg)}개 (Google Sheet from Secrets)")

# LLM 호출
llm = OpenAILLM(api_key=api_key)

st.divider()
progress = st.progress(0)
results: List[Dict[str, Any]] = []
for i, issue in enumerate(issues_cfg, start=1):
    issue_id = issue.get("id") or f"issue_{i}"
    title = issue.get("title", issue_id)
    definition = issue.get("definition", "")
    data = llm.review(model=model, issue_id=issue_id, issue_definition=definition, full_text=raw_text)
    data.setdefault("title", title)
    results.append(data)
    progress.progress(int(i/len(issues_cfg)*100))
progress.empty()

found_list = [r for r in results if r.get("found")]

left, right = st.columns([2, 1])

with right:
    st.subheader("🔎 탐지 요약")
    st.write(f"총 이슈 유형: {len(results)} / 발견: {len(found_list)}")
    if found_list:
        for r in found_list:
            with st.expander(f"✅ {r.get('title', r['issue_id'])}"):
                st.markdown(f"**설명:** {r.get('explanation','')}")
                idxs = r.get("clause_indices", [])
                if idxs:
                    st.markdown("**관련 조항 인덱스:** " + ", ".join(map(str, idxs)))
                quotes = r.get("evidence_quotes", [])
                if quotes:
                    st.markdown("**근거 인용문:**")
                    for q in quotes:
                        st.markdown(f"> {q}")
    else:
        st.info("탐지된 문제 없음(또는 모델이 탐지하지 못함).")

    st.markdown("---")
    st.download_button(
        "📥 결과(JSON) 다운로드",
        data=json.dumps(results, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name=f"contract_review_results_{int(time.time())}.json",
        mime="application/json",
    )

with left:
    st.subheader("📄 문서 보기")
    st.caption("우측 이슈 요약을 참고하여 관련 조항을 확인하세요.")

    # 하이라이트: 이슈가 보고한 clause_indices를 노란 박스로 표시
    highlight_indices = sorted({idx for r in found_list for idx in r.get("clause_indices", []) if isinstance(idx, int)})

    def render_clause_html(c: Clause, highlight: bool) -> str:
        safe = html.escape(c.text)
        bg = "#fffbe6" if highlight else "#f6f7f9"
        border = "1px solid #ffe58f" if highlight else "1px solid #e5e7eb"
        return (
            f"<div style='padding:8px; margin:8px 0; border-radius:12px; background:{bg}; border:{border}'>"
            f"<div style='font-weight:600'>{html.escape(c.title)}</div>"
            f"<div style='white-space:pre-wrap'>{safe}</div>"
            f"</div>"
        )

    html_parts = [render_clause_html(c, c.idx in highlight_indices) for c in clauses]
    st.markdown("\n".join(html_parts), unsafe_allow_html=True)

st.success("분석이 완료되었습니다. 좌/우 패널을 참고해 주세요.")
