"""인사팀(hr_admin) 의도 분류 평가: 관리자 골든셋으로 의도·집계 도구·인자를 채점해요 (답변 생성은 안 함).

    python scripts/eval_admin_intent.py
    python scripts/eval_admin_intent.py --split test --save

의도 분류는 규칙만 써요 (LLM은 도구 호출 뒤 답변 생성에서만 씀).
- 정답 data와 both는 같은 정답으로 봐요 (backend는 집계 도구를 쓰면 항상 규정도 같이 찾음)
- 질문 내용이 없으면 파이프라인처럼 no_question으로 봐요
- 정답이 refuse인 문항(A-L03·L04 대화 기록 조회, 117번 일반 직원 계정)은 인사팀 의도에 refuse가 없어 채점에서 빼고 따로 보여줘요
"""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import admin_tools  # noqa: E402
from app.graph import nodes  # noqa: E402
from app.graph.admin_classify import classify_admin_state  # noqa: E402

GOLDEN_PATH = ROOT / "eval" / "golden" / "admin_golden_set.json"
RUNS_DIR = ROOT / "eval" / "golden" / "runs"
LOGIN = "E1307"   # 골든셋을 만들 때 쓴 인사팀 계정


def load_items(path: Path = GOLDEN_PATH) -> tuple[list[dict], date]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [i for i in data["items"] if i["role"] == "hr_admin"], date.fromisoformat(data["as_of"])


def same_intent(v):
    return "both" if v == "data" else v


def _norm(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():   # 10과 10.0은 같은 값
        v = int(v)
    return str(v)


def predict(question: str, today: date) -> dict:
    s = {"employee_id": LOGIN, "role": "hr_admin", "question": question}
    s.update(nodes.guard(s))
    if not nodes.has_question_content(s["masked_question"]):
        return {"intent": "no_question", "admin_tool": None, "admin_params": {}}
    r = classify_admin_state(s, today)
    return {"intent": r["intent"], "admin_tool": r.get("admin_tool"), "admin_params": r.get("admin_params") or {},
            "sensitive_topic": r.get("sensitive_topic")}


def _prf(rows, label):
    tp = sum(r["gold_intent"] == label and r["pred_intent"] == label for r in rows)
    fp = sum(r["gold_intent"] != label and r["pred_intent"] == label for r in rows)
    fn = sum(r["gold_intent"] == label and r["pred_intent"] != label for r in rows)
    p = tp / (tp + fp) if tp + fp else 0.0
    rc = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": round(p, 3), "recall": round(rc, 3), "f1": round(2 * p * rc / (p + rc), 3) if p + rc else 0.0,
            "n": tp + fn}


def _rate(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def summarize(rows: list[dict]) -> dict:
    scored = [r for r in rows if r["scored"]]
    labels = sorted({r["gold_intent"] for r in scored})
    per_label = {L: _prf(scored, L) for L in labels}
    tool_rows = [r for r in scored if r["gold_tool"]]
    impl_rows = [r for r in tool_rows if r["gold_tool"] in admin_tools.ADMIN_TOOLS]
    ms = [r["ms"] for r in rows]
    return {
        "문항": len(scored),
        "의도 정확도": _rate([r["intent_ok"] for r in scored]),
        "의도 Macro-F1": round(sum(v["f1"] for v in per_label.values()) / len(per_label), 3) if per_label else None,
        "도구 정확도": _rate([r["tool_ok"] for r in tool_rows]),
        "인자 일치율": _rate([r["params_ok"] for r in tool_rows]),
        "구현 도구 문항": len(impl_rows),
        "구현 도구 정확도": _rate([r["tool_ok"] for r in impl_rows]),
        "구현 도구 인자 일치율": _rate([r["params_ok"] for r in impl_rows]),
        "도구 오탐(정답에 도구 없음)": sum(1 for r in scored if not r["gold_tool"] and r["pred_tool"]),
        "평균 ms": round(sum(ms) / len(ms), 1) if ms else None,
        "최대 ms": round(max(ms), 1) if ms else None,
        "라벨별": per_label,
    }


def run(split: str = "all", items=None, as_of: date | None = None) -> dict:
    """채점 결과: {"summary": {...}, "by_split": {...}, "rows": [...]}"""
    if items is None:
        items, golden_as_of = load_items()
        as_of = as_of or golden_as_of
    rows = []
    for it in (i for i in items if split == "all" or i["split"] == split):
        t = perf_counter()
        try:
            p = predict(it["question"], as_of)
        except Exception as e:   # 한 문항 오류로 전체가 멈추지 않게
            p = {"intent": f"error:{type(e).__name__}", "admin_tool": None, "admin_params": {}}
        ms = (perf_counter() - t) * 1000
        gold = same_intent(it["intent"])
        gp = it.get("params") or {}
        tool_ok = (p["admin_tool"] == it["tool"]) if it["tool"] else None
        params_ok = (all(_norm(p["admin_params"].get(k)) == _norm(v) for k, v in gp.items())
                     if tool_ok else (False if it["tool"] else None))
        rows.append({"id": it["id"], "split": it["split"], "question": it["question"],
                     "gold_intent": gold, "pred_intent": same_intent(p["intent"]),
                     "gold_tool": it["tool"], "pred_tool": p["admin_tool"],
                     "gold_params": gp, "pred_params": {k: _norm(v) for k, v in p["admin_params"].items()},
                     "intent_ok": same_intent(p["intent"]) == gold, "tool_ok": tool_ok, "params_ok": params_ok,
                     "scored": gold != "refuse", "ms": round(ms, 1)})
    by_split = {sp: summarize([r for r in rows if r["split"] == sp]) for sp in sorted({r["split"] for r in rows})}
    return {"summary": summarize(rows), "by_split": by_split, "rows": rows}


def markdown(result: dict) -> str:
    s = result["summary"]
    lines = ["| 문항 | 의도 정확도 | Macro-F1 | 도구 정확도 | 구현 도구 정확도 | 구현 도구 인자 | 도구 오탐 | 평균 ms |",
             "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
             f"| {s['문항']} | {s['의도 정확도']} | {s['의도 Macro-F1']} | {s['도구 정확도']} | {s['구현 도구 정확도']} ({s['구현 도구 문항']}) | "
             f"{s['구현 도구 인자 일치율']} | {s['도구 오탐(정답에 도구 없음)']} | {s['평균 ms']} |",
             "", "| 라벨 | 정밀도 | 재현율 | F1 | 문항 |", "| --- | ---: | ---: | ---: | ---: |"]
    for label, v in s["라벨별"].items():
        lines.append(f"| {label} | {v['precision']} | {v['recall']} | {v['f1']} | {v['n']} |")
    missing = sorted({x["gold_tool"] for x in result["rows"] if x["gold_tool"] and x["gold_tool"] not in admin_tools.ADMIN_TOOLS})
    if missing:
        lines += ["", f"골든셋 정답 도구 중 backend에 없는 도구 {len(missing)}개 (도구 정확도에서 항상 틀림): " + ", ".join(missing)]
    refused = [x for x in result["rows"] if not x["scored"]]
    if refused:
        lines += ["", "채점 제외 (정답 refuse, 인사팀 의도에 refuse 없음):"]
        lines += [f"- {x['id']} → {x['pred_intent']} / {x['pred_tool']}: {x['question']}" for x in refused]
    return "\n".join(lines)


def save(result: dict, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or RUNS_DIR / f"admin_intent_{datetime.now():%Y%m%d_%H%M%S}_rule"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {k: v for k, v in result.items() if k != "rows"}
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (out_dir / "route_rule.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False, default=str) + "\n" for x in result["rows"]), encoding="utf-8")
    (out_dir / "summary.md").write_text(markdown(result), encoding="utf-8")
    return out_dir


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", choices=("all", "dev", "test", "uncertain"), default="all")
    ap.add_argument("--save", action="store_true", help="eval/golden/runs/에 결과 저장")
    args = ap.parse_args()
    result = run(args.split)
    print(markdown(result))
    wrong = [x for x in result["rows"] if x["scored"] and (not x["intent_ok"] or x["tool_ok"] is False)]
    print(f"\n틀린 문항 {len(wrong)}개")
    for x in wrong[:40]:
        print(f"- {x['id']} 정답 {x['gold_intent']}/{x['gold_tool']} → {x['pred_intent']}/{x['pred_tool']}: {x['question']}")
    if args.save:
        print("\n저장:", save(result))


if __name__ == "__main__":
    main()
