"""보안문서 판단 평가: 질문에 노사회의록·징계회의록이 필요한지 (규칙 vs 규칙+① LLM).

    python scripts/eval_conf_doc.py           # 규칙만
    python scripts/eval_conf_doc.py --llm     # 규칙 vs 규칙+LLM (.env의 로컬 LLM)

정답 파일 eval/conf_doc/conf_doc_cases.jsonl 은 LLM 프롬프트 예시(app/graph/conf_doc._LLM_EXAMPLES)와 겹치지 않아요.
- 재현율: 회의록이 필요한 질문 중 맞는 문서로 표시한 비율 (놓치면 일반 규정 답변으로 감)
- 오탐: 규정 질문인데 보안문서로 표시한 수 (권한 없는 직원은 규정 질문에 거부 안내를 받음)
"""
import argparse
import json
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.graph import conf_doc  # noqa: E402

CASES_PATH = ROOT / "eval" / "conf_doc" / "conf_doc_cases.jsonl"


def run(use_llm: bool) -> dict:
    cases = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    config.CLASSIFY_LLM = use_llm
    rows = []
    for case in cases:
        started = perf_counter()
        pred = (conf_doc.classify if use_llm else conf_doc.detect)(case["question"]).get("conf_doc") or "none"
        ms = (perf_counter() - started) * 1000
        gold = case["doc"]
        ok = pred == gold or (case.get("either") and pred != "none")
        rows.append({**case, "pred": pred, "ok": bool(ok), "ms": round(ms, 1)})
    pos = [r for r in rows if r["doc"] != "none"]
    neg = [r for r in rows if r["doc"] == "none"]
    return {
        "rows": rows,
        "정확도": sum(r["ok"] for r in rows) / len(rows),
        "재현율": sum(r["ok"] for r in pos) / len(pos),
        "놓침": [r for r in pos if r["pred"] == "none"],
        "문서 틀림": [r for r in pos if r["pred"] != "none" and not r["ok"]],
        "오탐": [r for r in neg if r["pred"] != "none"],
        "평균 ms": sum(r["ms"] for r in rows) / len(rows),
        "n_pos": len(pos), "n_neg": len(neg),
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--llm", action="store_true")
    args = ap.parse_args()
    results = {"규칙": run(False)}
    if args.llm:
        if config.USE_FAKE_LLM:
            raise SystemExit("--llm은 .env에 LLM_BASE_URL이 있어야 해요")
        results["규칙+LLM"] = run(True)
    print("| 방식 | 정확도 | 재현율 | 놓침 | 문서 틀림 | 오탐 | 평균 ms |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for name, r in results.items():
        print(f"| {name} | {r['정확도']:.1%} | {r['재현율']:.1%} | {len(r['놓침'])}/{r['n_pos']} | "
              f"{len(r['문서 틀림'])} | {len(r['오탐'])}/{r['n_neg']} | {r['평균 ms']:.0f} |")
    for name, r in results.items():
        for kind in ("놓침", "문서 틀림", "오탐"):
            for x in r[kind]:
                print(f"- [{name}] {kind} {x['id']} 정답 {x['doc']} → {x['pred']}: {x['question']}")


if __name__ == "__main__":
    main()
