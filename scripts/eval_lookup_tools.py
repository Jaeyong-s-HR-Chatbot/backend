"""임직원 본인 조회(lookup·both) 도구 선택 평가. DB·LLM 없이 돌아요.

    python scripts/eval_lookup_tools.py                      # eval/golden/golden_set.json
    python scripts/eval_lookup_tools.py --golden ../RAG-hyerim/eval/golden/golden_set.json

정답: 골든셋 문항의 needs_hr_data·expected_hr_tables (hr.family → family 도구 등).
- 도구 맞힘: 정답 테이블의 도구를 app/tools.py 키워드가 전부 고르면 맞힘 (hr.employees = profile은 항상 실행이라 제외)
- 조회 경로: 규칙 분류가 lookup·both라 조회가 실제로 실행되는지 (rule·out_of_scope면 LLM이 정하므로 따로 셈)"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import tools
from app.graph import nodes

TABLE_TO_TOOL = {
    "hr.leave_balance": "leave_balance", "hr.welfare_points": "welfare_points", "hr.payroll": "payroll",
    "hr.family": "family", "hr.requests": "requests", "hr.training": "training",
}


def picked_tools(question: str) -> set[str]:
    return {name for words, name, _ in tools.TOOL_KEYWORDS if tools.tool_matches(name, words, question)}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(ROOT / "eval" / "golden" / "golden_set.json"))
    args = ap.parse_args()
    items = json.loads(Path(args.golden).read_text(encoding="utf-8"))["items"]
    targets = [it for it in items if it.get("needs_hr_data")]

    hit, route, wrong = 0, Counter(), []
    for it in targets:
        q = it["question"]
        exp = {TABLE_TO_TOOL[t] for t in it.get("expected_hr_tables") or [] if t in TABLE_TO_TOOL}
        got = picked_tools(q)
        intent = nodes._classify_rule({"masked_question": q, "question": q, "role": "employee"})["intent"]
        route[intent] += 1
        if exp <= got:
            hit += 1
        else:
            wrong.append((it["id"], q, sorted(exp), sorted(got), intent))

    n = len(targets)
    print(f"본인 데이터가 필요한 문항: {n}")
    print(f"- 도구 맞힘: {hit}/{n} ({hit / n:.1%})" if n else "- 문항 없음")
    print(f"- 규칙 의도: {dict(route)} (lookup·both만 조회 실행, rule·out_of_scope는 LLM이 정함)")
    if wrong:
        print("\n| id | 질문 | 정답 도구 | 고른 도구 | 규칙 의도 |\n| --- | --- | --- | --- | --- |")
        for row in wrong:
            print("| " + " | ".join(str(c) for c in row) + " |")


if __name__ == "__main__":
    main()
