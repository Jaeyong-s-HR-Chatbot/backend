"""파이프라인 (팀 설계도 기준, event_plan·draft는 아직 없음). 프레임워크 없이 함수로 순서대로 실행해요.

guard → classify ─┬─ rule ──────────────→ retrieve → calc → generate → verify ─┬─ 통과 → log
                  ├─ lookup ─→ lookup ─────────────────────→ generate          ├─ 근거 부족 → 다시 검색 (1회)
                  ├─ both/event → lookup → retrieve                             └─ 그래도 실패 → refuse → log
                  ├─ sensitive → sensitive → log
                  └─ refuse (타인 조회) → refuse → log
"""
from time import perf_counter

from . import nodes as n
from .state import HRState


def run_pipeline(employee_id: str, role: str, question: str, own_name: str = "") -> HRState:
    started = perf_counter()
    s: HRState = {
        "employee_id": employee_id,
        "role": role,
        "own_name": own_name,
        "question": question,
    }
    s |= n.guard(s)
    s.pop("question", None)
    del question
    s |= n.classify(s)

    if s["intent"] == "sensitive":
        s |= n.sensitive(s)
    elif s["intent"] == "refuse":
        s |= n.refuse(s)
    else:
        if s["intent"] in ("lookup", "both", "event"):
            s |= n.lookup(s)
        if s["intent"] == "lookup":
            s |= n.generate(s)
            s |= n.verify(s)
        else:
            for _ in range(n.MAX_RETRIES + 1):          # 처음 1번 + 재검색 1번
                s |= n.retrieve(s)
                s |= n.calc(s)
                s |= n.generate(s)
                s |= n.verify(s)
                if s["verified"]:
                    break
        if not s.get("verified"):
            s |= n.refuse(s)

    s |= n.mask_response(s)
    s["latency_ms"] = int((perf_counter() - started) * 1000)
    s |= n.write_log(s)
    return s
