"""파이프라인 (팀 설계도 기준, event_plan·draft는 아직 없음). 프레임워크 없이 함수로 순서대로 실행해요.

직원(employee)
guard → classify ─┬─ rule ──────────────→ retrieve → calc → generate → verify ─┬─ 통과 → log
                  ├─ lookup ─→ lookup ─────────────────────→ generate          ├─ 근거 부족 → 다시 검색 (1회)
                  ├─ both ──→ lookup → retrieve                                 └─ 그래도 실패 → refuse → log
                  ├─ sensitive → sensitive → log
                  ├─ out_of_scope (인사 규정과 무관) → 안내 → log
                  └─ refuse (타인 조회) → refuse → log

인사팀(hr_admin, 역할은 토큰에서) — classify가 admin_classify로 분류
guard → classify ─┬─ rule ─────────────────→ retrieve → calc → generate → verify (직원과 같음)
                  ├─ data ─→ admin_lookup ──────────────→ generate (+집계 표) → verify
                  ├─ both ─→ admin_lookup → retrieve → generate (+집계 표) → verify
                  │            └ 규정 근거를 못 찾아도 집계 표는 답함 ("근거 못 찾음" 표시)
                  ├─ lookup (본인 데이터) ─→ lookup → generate (직원과 같음)
                  ├─ both (본인 데이터 + 규정, 집계 도구 없음) ─→ lookup → retrieve → … (직원과 같음)
                  └─ out_of_scope (LLM 보조 분류에서만) → 안내 → log
"""
from time import perf_counter

from . import nodes as n
from . import conf_chat
from .state import HRState


def run_pipeline(employee_id: str, role: str, question: str, own_name: str = "") -> HRState:
    started = perf_counter()
    s: HRState = {
        "employee_id": employee_id,
        "role": role,
        "own_name": own_name,
        "question": question,
    }
    s |= n.guard(s)          # 질문은 원문 그대로 처리하고, 마스킹 사본은 로그 저장에만 써요
    s |= n.classify(s)

    if s["intent"] == "sensitive":
        s |= n.sensitive(s)
    elif s.get("refuse_reason") == "chat_log":
        s |= n.refuse(s)
    elif s.get("conf_doc"):
        s |= conf_chat.answer(s)
    elif s["intent"] == "refuse":
        s |= n.refuse(s)
    elif s["intent"] == "no_question" or not n.has_question_content(s["masked_question"]):
        s |= n.refuse_no_question(s)
    elif s["intent"] == "out_of_scope":
        s |= n.out_of_scope(s)
    else:
        if s["intent"] in ("lookup", "both") and not s.get("admin_tool"):   # 본인 조회 (인사팀 본인 질문 포함)
            s |= n.lookup(s)
        if s["intent"] in ("data", "both") and s.get("admin_tool"):
            s |= n.admin_lookup(s)
        if s["intent"] in ("lookup", "data"):
            s |= n.generate(s)
            s |= n.verify(s)
        else:
            for _ in range(n.MAX_RETRIES + 1):          # 처음 1번 + 재검색 1번
                s |= n.retrieve(s)
                s |= n.calc(s)
                s |= n.generate(s)
                s |= n.normalize_no_evidence(s)
                if s.get("kind") == "refused":
                    break
                s |= n.verify(s)
                if s["verified"]:
                    break
            if not s.get("verified") and s.get("admin_result") is not None:
                # 인사팀: 맞는 규정을 못 찾았어도 집계 표는 돌려줌 (단순 현황 질문은 원래 규정이 필요 없어요)
                s |= {"chunks": [], "verified": True, "kind": "answer",
                      "answer": "집계 결과를 알려 드려요.\n\n" + n._admin_table(s["admin_result"])}
        if not s.get("verified") and s.get("kind") != "refused":
            s |= n.refuse(s)

    s |= n.mask_response(s)
    s["latency_ms"] = int((perf_counter() - started) * 1000)
    s |= n.write_log(s)
    return s
