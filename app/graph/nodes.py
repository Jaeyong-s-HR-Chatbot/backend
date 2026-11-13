"""파이프라인 단계(노드). DB·LLM 주소가 없으면 가짜 값으로 동작해요.

각 노드는 state를 받아 바뀐 값만 dict로 돌려줘요.
진짜로 바꿀 때는 함수 안만 고치고, 이름과 입출력(HRState 필드)은 그대로 둬요.
"""
import json
import logging
import re
from urllib.parse import urlsplit

import httpx

from .. import config
from ..fake_data import HR_RECORDS
from ..masking import mask_answer, mask_text, pseudonymize
from . import retriever
from .state import HRState

log = logging.getLogger("hr_chat")

VERIFY_MIN_SCORE = config.VERIFY_MIN_SCORE   # 근거 점수가 이보다 낮으면 재검색 → 그래도 낮으면 거절
MAX_RETRIES = 1

def guard(s: HRState) -> HRState:
    result = mask_text(s["question"], own_emp_id=s["employee_id"], own_name=s.get("own_name"))
    return {
        "masked_question": result.text,
        "pii_found": result.found,
        "has_other_emp_id": result.found.get("사번", 0) > 0,
        "retries": 0,
    }


# ───────────── classify: 의도 분류 (규칙) ─────────────
_SENSITIVE = ["괴롭힘", "성희롱", "성추행", "폭언", "징계", "고충", "따돌림", "부당"]
_OTHER_PERSON = ["다른 직원", "다른 사람", "남의 ", "옆팀", "옆 팀"]
_TITLE = r"(?:사원|주임|대리|과장|차장|부장|팀장|본부장|님|씨)"
_PRIVATE = r"(?:연봉|급여|월급|연차|수당|평가|인사고과|주민번호|계좌|주소|전화번호|휴직)"
_OTHER_NAME = re.compile(rf"[가-힣]{{2,4}}\s?{_TITLE}(?:님)?(?:의|은|는|이|가)?\s?{_PRIVATE}")
_EMP_ID = re.compile(r"(?<![A-Za-z0-9])[Ee]1\d{3}(?!\d)")
_SELF = ["내 ", "나의", "제 ", "저의", "내가", "제가", "남은", "남았", "잔여", "나 연차", "저 연차"]
_PERSONAL_IDENTIFIER_INTRO = re.compile(
    r"(?:제|내|저의|나의)\s*"
    r"(?:전화번호|번호|연락처|사번|이메일|메일|계좌번호|계좌|주민번호|카드번호)"
    r"\s*(?:은|는|이|가|을|를|의|에|으로|로)?\s*"
    r"\[(?:전화|본인사번|이메일|계좌|주민번호|카드)\]"
)
_EVENT = ["결혼", "출산", "이사", "사망", "부고", "돌아가", "육아휴직", "입학", "퇴직"]
_RULE_WORDS = ["규정", "기준", "조건", "받을 수", "어떻게", "방법", "절차", "계산", "왜"]
_TOOL_WORDS = ["연차", "휴가 며칠 남", "복지포인트", "복지 포인트", "포인트", "급여", "월급", "실수령", "통상임금", "수당 얼마 나왔",
               "가족수당", "부양가족", "신청", "결재", "서류", "교육", "이수"]   # app/tools.py의 조회 도구와 같은 단어


def classify(s: HRState) -> HRState:
    """TODO 교체: 규칙은 유지하고 애매한 것만 LLM 분류로 보완할지 결정. 인사팀(hr_admin) 전용 조회는 아직 없음"""
    q = s["masked_question"]
    intent_q = _PERSONAL_IDENTIFIER_INTRO.sub("", q)
    other_id = s.get("has_other_emp_id", False) or "[사번]" in q
    has_tool = any(w in intent_q for w in _TOOL_WORDS)
    is_self = any(w in intent_q for w in _SELF) or ("[본인사번]" in q and has_tool)
    asks_rule = any(w in intent_q for w in _RULE_WORDS)
    if any(w in intent_q for w in _SENSITIVE):
        intent = "sensitive"
    elif other_id or any(w in intent_q for w in _OTHER_PERSON) or _OTHER_NAME.search(intent_q):
        intent = "refuse"          # 타인 조회·권한 밖
    elif is_self and has_tool and not asks_rule:
        intent = "lookup"          # 내 데이터만 보면 되는 질문 (연차 잔여, 급여, 신청 현황 등)
    elif any(w in intent_q for w in _EVENT):
        intent = "event"
    elif is_self:
        intent = "both"            # 내 조건 + 규정 (조회 결과와 근거를 함께 봄)
    else:
        intent = "rule"
    return {"intent": intent}


# ───────────── lookup: 본인 조회 ─────────────
def lookup(s: HRState) -> HRState:
    """사번은 반드시 토큰에서 온 s['employee_id']만 사용. DB가 있으면 app/tools.py의 정해진 조회만 실행"""
    if config.USE_DB:
        from .. import tools
        data, used = tools.run_lookups(s["employee_id"], s["masked_question"])
    else:
        data = dict(HR_RECORDS.get(s["employee_id"], {}))
        used = list(data)
    return {"lookup": data, "tools_used": used}


# ───────────── retrieve: 규정 검색 ─────────────
def retrieve(s: HRState) -> HRState:
    """TODO 교체: 이재용 님 리트리버 (BGE-M3 + BM25 → RRF → bge-reranker-v2-m3)"""
    wide = s.get("retries", 0) > 0
    return {"chunks": retriever.search(s["masked_question"], k=5, wide=wide)}


# ───────────── calc: 계산 Tool ─────────────
def calc(s: HRState) -> HRState:
    """출장비: 근거에 단가(rates)가 있으면 박·일수로 계산. TODO: 기준표(rules.json) 기반 계산 함수로 확장 (수당, 경조휴가 등)"""
    trip = re.search(r"(\d+)\s*박\s*(\d+)\s*일", s["masked_question"])
    if "출장" in s["masked_question"] and trip:
        nights, days = (int(value) for value in trip.groups())
        for chunk in s.get("chunks", []):
            rates = chunk.get("rates")
            if rates:
                daily_allowance = rates["daily_allowance"] * days
                meal_allowance = rates["meal_allowance"] * days
                daily_total = daily_allowance + meal_allowance
                lodging_total = rates["lodging_cap"] * nights
                return {"calc": {
                    "daily_allowance": daily_allowance,
                    "meal_allowance": meal_allowance,
                    "daily_allowance_and_meal": daily_total,
                    "lodging_cap": lodging_total,
                    "maximum_total": daily_total + lodging_total,
                }}
    return {}


# ───────────── generate: 답변 생성 ─────────────
SYSTEM_PROMPT = """당신은 누리철도㈜ 인사(HR) 안내 챗봇입니다. 아래 규칙을 반드시 지키세요.
1. [근거]와 [조회 결과], [계산 결과]에 있는 내용으로만 답하세요. 근거에 없는 내용, 금액, 일수는 지어내지 마세요.
2. 근거에서 답을 찾을 수 없으면 "규정에서 확인되지 않아요. 인사팀에 확인해 주세요."라고 답하세요.
3. 질문의 전제가 근거와 다르면 먼저 "아니요"라고 바로잡고 근거의 내용을 알려 주세요.
4. [조회 결과]의 조건(근속, 직급, 가족, 나이)을 근거의 기준에 하나씩 대입해 답하세요.
5. 받을 수 있는 것이 여러 개면 휴가·금액·지원·신청 기한을 빠짐없이 항목별로 쓰세요.
6. 금액이나 일수를 계산할 때는 계산식과 결과를 함께 쓰세요.
7. '각'으로 표시된 금액은 항목마다 따로 계산한 뒤 합산하세요. [계산 결과]가 있으면 다시 계산하지 말고 그 값을 그대로 쓰세요.
8. 근거에 [예시 설명]이 있으면 그 금액 산출 결과를 답변의 기준으로 따르세요.
9. 한국어로 간결하게 쓰고, 숫자(일수·금액·비율)는 근거·조회 결과와 똑같이 쓰세요. 조문 번호는 답 끝에 괄호로 적으세요."""


def _won(n):
    return f"{int(n):,}원"


def _lookup_sentences(lk: dict) -> list[str]:
    """조회 결과를 문장으로 (가짜 LLM용). 값은 조회 결과 그대로"""
    out = []
    if "연차" in lk:
        a = lk["연차"]
        out.append(f"{a['연도']}년 연차는 {a['부여일수']}일 중 {a['사용일수']:g}일을 써서 {a['잔여일수']:g}일 남았어요. ({a['기준일']} 기준)")
    if "복지포인트" in lk:
        w = lk["복지포인트"]
        out.append(f"{w['연도']}년 복지포인트는 {w['부여포인트']:,}점 중 {w['사용포인트']:,}점을 써서 {w['잔여포인트']:,}점 남았어요. {w['만료일']}까지 쓸 수 있어요.")
    if "급여" in lk:
        p = lk["급여"]
        out.append(f"{p['급여월']} 급여는 지급총액 {_won(p['지급총액'])}, 실수령액 {_won(p['실수령액'])}이에요. "
                   f"시간외근무 {p['시간외근무시간']:g}시간에 시간외수당 {_won(p['시간외수당'])}, 가족수당 {_won(p['가족수당'])}이 들어 있어요. (지급일 {p['지급일']})")
    if "가족" in lk:
        f = lk["가족"]
        out.append("등록된 가족은 " + ", ".join(f"{x['관계']}({x['나이']}세{', 부양' if x['부양가족'] else ''})" for x in f["가족"])
                   + f"이고, 부양가족은 {f['부양가족수']}명이에요.")
    if "신청현황" in lk:
        out.append("최근 신청은 " + ", ".join(f"{r['신청일']} {r['종류']}({r['내용']}) {r['상태']}" for r in lk["신청현황"]["최근신청"][:3]) + "이에요.")
    if "교육" in lk:
        t = lk["교육"]
        out.append(("아직 이수하지 않은 필수교육은 " + ", ".join(t["필수교육_미이수"]) + "이에요.") if t["필수교육_미이수"]
                   else f"필수교육은 모두 이수했어요. (전체 {t['전체과정수']}개 과정 중 {t['이수과정수']}개 이수)")
    return out


def _fake_answer(s: HRState) -> str:
    lk, chunks = s.get("lookup", {}), s.get("chunks", [])
    parts = _lookup_sentences(lk) if s.get("intent") in ("lookup", "both", "event") else []
    if s.get("intent") == "lookup" and not parts and lk.get("기본정보"):
        b = lk["기본정보"]
        parts.append(f"{b.get('부서', '')} {b.get('직위', '')}({b.get('직급', '')}), 입사일 {b.get('입사일', '')}, 근속 {b.get('근속연수', '')}년이에요.")
    if chunks and s.get("intent") != "lookup":
        top = chunks[0]
        parts.append(top.get("canned") or f"[가짜 LLM] 가장 가까운 근거는 '{top['citation']}'이에요: {top['text'][:150]}")
    return " ".join(parts)


def _clean(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)     # 생각 과정이 섞여 나오면 제거
    return text.split("</think>")[-1].strip()


def _llm_answer(s: HRState) -> str:
    ctx = "\n\n".join(
        f"[{c['citation']}]\n{c['text']}"
        + (f"\n[예시 설명]\n{c['canned']}" if c.get("canned") else "")
        for c in s.get("chunks", [])[:5]
    )
    user_prompt = (
        f"[근거]\n{ctx or '(없음)'}\n\n"
        f"[조회 결과]\n{json.dumps(s.get('lookup', {}), ensure_ascii=False)}\n\n"
        f"[계산 결과]\n{json.dumps(s.get('calc', {}), ensure_ascii=False)}\n\n"
        f"[질문]\n{s['masked_question']}"
    )
    base = urlsplit(config.LLM_BASE_URL)
    if base.scheme == "http" and base.hostname in {"localhost", "127.0.0.1"} \
            and base.port == 11434 and base.path.rstrip("/") == "/v1":
        # Ollama의 OpenAI 호환 주소는 이 모델에 think=false가 적용되지 않아서, Ollama 고유 주소(/api/chat)로 호출
        response = httpx.post(
            f"{base.scheme}://{base.netloc}/api/chat",
            json={
                "model": config.LLM_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "think": False,
                "options": {"temperature": 0},
            },
            timeout=config.LLM_TIMEOUT,
        )
        response.raise_for_status()
        return _clean(response.json()["message"]["content"])

    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, model=config.LLM_MODEL,
                     temperature=0, timeout=config.LLM_TIMEOUT, max_retries=1, max_tokens=600)
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", "{user_prompt}"),
    ])
    msg = (prompt | llm).invoke({"user_prompt": user_prompt})
    return _clean(msg.content)


def generate(s: HRState) -> HRState:
    answer = _fake_answer(s) if config.USE_FAKE_LLM else _llm_answer(s)
    return {"answer": answer}


def mask_response(s: HRState) -> HRState:
    answer = s.get("answer", "")
    public_answer = mask_answer(answer).text
    answer_masked = None
    if not s.get("tools_used"):
        answer_masked = mask_answer(
            answer,
            own_emp_id=s["employee_id"],
            own_name=s.get("own_name"),
        ).text
    return {"answer": public_answer, "answer_masked": answer_masked}


# ───────────── verify: 근거 검증 ─────────────
def verify(s: HRState) -> HRState:
    """TODO 교체: 답변 숫자 ↔ 근거·계산 숫자 대조 + 리랭커 점수"""
    if s.get("intent") == "lookup":
        return {"verified": bool(s.get("lookup")) and bool(s.get("answer"))}
    chunks = s.get("chunks", [])
    ok = bool(chunks) and chunks[0]["score"] >= VERIFY_MIN_SCORE and bool(s.get("answer"))
    return {"verified": ok, "retries": s.get("retries", 0) + (0 if ok else 1)}


def route_after_verify(s: HRState) -> str:
    if s.get("verified"):
        return "log"
    return "retrieve" if s.get("retries", 0) <= MAX_RETRIES else "refuse"


# ───────────── refuse / sensitive ─────────────
def refuse(s: HRState) -> HRState:
    if s.get("intent") == "refuse":
        msg = "다른 직원의 정보나 권한 밖의 내용은 알려드릴 수 없어요. 필요하면 인사팀에 정식으로 요청해 주세요."
    else:
        msg = "이 질문은 등록된 규정에서 근거를 찾지 못했어요. 정확한 내용은 인사팀에 확인해 주세요."
    return {"answer": msg, "kind": "refused", "chunks": []}


def sensitive(s: HRState) -> HRState:
    return {
        "answer": ("이 주제는 챗봇이 판단하지 않아요. 고충처리 담당자나 인사팀 상담 창구로 직접 상담해 주세요. "
                   "신고 내용은 비밀이 보장되는 절차로 처리돼요."),
        "kind": "sensitive",
        "chunks": [],
    }


# ───────────── log: 감사 로그 ─────────────
def write_log(s: HRState) -> HRState:
    """가명 ID와 마스킹한 내용만 콘솔·DB 로그에 저장한다."""
    kind = s.get("kind") or "answer"
    question_masked = "[민감 주제 문의]" if s.get("intent") == "sensitive" else s["masked_question"]
    payload = {
        "user_pseudo_id": pseudonymize(s["employee_id"]),
        "question_masked": question_masked,
        "answer_masked": s.get("answer_masked") if not s.get("tools_used") else None,
        "pii_found": s.get("pii_found", {}),
        "intent": s.get("intent"),
        "kind": kind,
        "cited_chunks": [c["id"] for c in s.get("chunks", [])[:3] if c.get("id")],
        "tools_used": s.get("tools_used", []),
        "latency_ms": s.get("latency_ms"),
    }
    log.info(json.dumps(payload, ensure_ascii=False))
    if config.USE_DB and config.CHAT_LOG_DB:
        try:
            from .. import db
            db.insert_chat_log(**payload)
        except Exception as exc:
            log.warning("DB 대화 로그 저장 실패: %s", type(exc).__name__)
    return {"kind": kind}
