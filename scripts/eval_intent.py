"""임직원 의도 분류 평가 (답변 생성은 안 함).

    python scripts/eval_intent.py --split dev            # 규칙만
    python scripts/eval_intent.py --split dev --llm      # 규칙 vs 규칙+분류 보조 LLM (.env의 LLM, 같은 문장으로 비교)

--llm이면 few-shot 예시(app/data/classify_examples.json)와 같은 쌍의 문장은 두 방식 모두 채점에서 빼요.
LLM 응답은 eval/intent/cache에 저장해 다시 돌릴 때 재사용해요."""
import argparse
import csv
import json
import re
import sys
from contextlib import nullcontext
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import calc_rules, config, tools
from app.fake_data import SAMPLE_CHUNKS
from app.graph import llm_classify, nodes

DATA_PATH = ROOT / "eval" / "intent" / "intent_cases.jsonl"
REVIEW_PATH = ROOT / "eval" / "intent" / "intent_cases_review.csv"
LABELS = ("rule", "lookup", "both", "sensitive", "refuse", "no_question", "out_of_scope")
ID_PREFIXES = (*LABELS, "event")   # event 라벨은 2026-11-18 폐지, 문장 id 앞부분에만 남음
REVIEW_COLUMNS = (
    "id",
    "split",
    "question",
    "intent",
    "tools",
    "calc_type",
    "calc_slots",
    "needs_review",
    "note",
    "수정_intent",
    "수정_메모",
)
CACHE_PATH = ROOT / "eval" / "intent" / "cache"
EXAMPLES_PATH = ROOT / "app" / "data" / "classify_examples.json"
EMPLOYEE_ID = "E1584"
OWN_NAME = "이서준"


def _read_cases():
    cases = []
    seen = set()
    for line_number, line in enumerate(DATA_PATH.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{DATA_PATH}:{line_number}: JSON 오류: {exc.msg}") from exc
        required = {
            "id", "question", "intent", "tools", "calc", "split", "source",
            "needs_review", "note",
        }
        if set(case) != required:
            raise ValueError(f"{DATA_PATH}:{line_number}: 필드가 기준과 다릅니다.")
        if case["id"] in seen:
            raise ValueError(f"{DATA_PATH}:{line_number}: 중복 id {case['id']}")
        if case["intent"] not in LABELS or case["split"] not in {"dev", "test"}:
            raise ValueError(f"{DATA_PATH}:{line_number}: intent 또는 split 값이 잘못됐습니다.")
        if case["source"] not in {"existing_test", "demo", "new"}:
            raise ValueError(f"{DATA_PATH}:{line_number}: source 값이 잘못됐습니다.")
        if not isinstance(case["tools"], list) or not isinstance(case["needs_review"], bool):
            raise ValueError(f"{DATA_PATH}:{line_number}: tools 또는 needs_review 형식이 잘못됐습니다.")
        calc = case["calc"]
        if calc is not None and (
            not isinstance(calc, dict)
            or set(calc) != {"type", "slots"}
            or not isinstance(calc["slots"], dict)
        ):
            raise ValueError(f"{DATA_PATH}:{line_number}: calc 형식이 잘못됐습니다.")
        seen.add(case["id"])
        cases.append(case)
    _validate_dataset(cases)
    return cases


def _validate_dataset(cases):
    pair_splits = {}
    label_counts = {label: {"dev": 0, "test": 0} for label in LABELS}
    reviewed_labels = False
    for case in cases:
        label_counts[case["intent"]][case["split"]] += 1
        match = re.fullmatch(r"([a-z_]+)-(\d{2})-[ab]", case["id"])
        if not match or match.group(1) not in ID_PREFIXES:
            raise ValueError(f"{case['id']}: pair 형식이 아닙니다.")
        reviewed_labels |= match.group(1) != case["intent"]
        pair_id = f"{match.group(1)}-{match.group(2)}"
        previous = pair_splits.setdefault(pair_id, case["split"])
        if previous != case["split"]:
            raise ValueError(f"{pair_id}: 변형 문장이 서로 다른 split에 있습니다.")
    if len(cases) < 140 or len(cases) > 200:
        raise ValueError(f"데이터 문장 수가 140~200 범위가 아닙니다: {len(cases)}")
    if not reviewed_labels:
        for label, counts in label_counts.items():
            if sum(counts.values()) < 15 or counts != {"dev": 12, "test": 8}:
                raise ValueError(f"{label}: 라벨별 문장 수 또는 60/40 split이 맞지 않습니다: {counts}")


def _write_cases(cases):
    payload = "".join(json.dumps(case, ensure_ascii=False, separators=(",", ":")) + "\n" for case in cases)
    DATA_PATH.write_text(payload, encoding="utf-8", newline="\n")


def _apply_review():
    cases = _read_cases()
    by_id = {case["id"]: case for case in cases}
    seen_rows = set()
    with REVIEW_PATH.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if tuple(reader.fieldnames or ()) != REVIEW_COLUMNS:
            raise ValueError(f"{REVIEW_PATH}: CSV 열이 기준과 다릅니다.")
        edits = {}
        for row_number, row in enumerate(reader, 2):
            case_id = row["id"]
            if case_id not in by_id:
                raise ValueError(f"{REVIEW_PATH}:{row_number}: 알 수 없는 id {case_id}")
            if case_id in seen_rows:
                raise ValueError(f"{REVIEW_PATH}:{row_number}: 중복 id {case_id}")
            seen_rows.add(case_id)
            corrected_intent = (row["수정_intent"] or "").strip()
            corrected_note = (row["수정_메모"] or "").strip()
            if corrected_intent and corrected_intent not in LABELS:
                raise ValueError(f"{REVIEW_PATH}:{row_number}: 수정_intent 값이 잘못됐습니다.")
            if corrected_intent or corrected_note:
                edits[case_id] = (corrected_intent, corrected_note)
        if seen_rows != set(by_id):
            raise ValueError(f"{REVIEW_PATH}: JSONL과 CSV의 id 목록이 다릅니다.")
    for case_id, (corrected_intent, corrected_note) in edits.items():
        case = by_id[case_id]
        if corrected_intent:
            case["intent"] = corrected_intent
            case["needs_review"] = False
        if corrected_note:
            case["note"] = corrected_note
    _write_cases(cases)
    print(f"검수 반영: {len(edits)}건")


def _normalize_number(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _business_trip_slots(question):
    match = re.search(r"(?<!\d)(\d+)\s*박\s*(\d+)\s*일", question)
    if not match:
        return None
    region = re.search(r"서울|부산|대구|인천|광주|대전|울산|세종|광역시", question)
    if not region:
        return None
    return {
        "nights": int(match.group(1)),
        "days": int(match.group(2)),
        "region": region.group(0),
    }


def _calc_prediction(masked_question, classifier_calc=None):
    if classifier_calc is not None:      # 분류 보조 LLM이 뽑은 계산 값
        return classifier_calc
    calc_state = {
        "masked_question": masked_question,
        "chunks": SAMPLE_CHUNKS,
    }
    result = nodes.calc(calc_state).get("calc")
    if not isinstance(result, dict):
        return None
    kind = result.get("종류")
    if kind == "연차 일수":
        inputs = result.get("입력", {})
        return {
            "type": "annual_leave",
            "slots": {
                "hire_date": inputs.get("입사일"),
                "as_of": inputs.get("기준일"),
            },
        }
    if kind == "시간외수당":
        inputs = result.get("입력", {})
        return {
            "type": "overtime",
            "slots": {
                "ordinary_wage": _normalize_number(inputs.get("통상임금")),
                "overtime_hours": _normalize_number(inputs.get("연장시간")),
                "night_hours": _normalize_number(inputs.get("야간시간")),
                "holiday_hours": _normalize_number(inputs.get("휴일시간")),
            },
        }
    if "maximum_total" in result:
        slots = _business_trip_slots(masked_question)
        return {"type": "business_trip", "slots": slots} if slots else None
    return None


def _selected_tools(intent, question):
    if intent not in {"lookup", "both"}:
        return []
    selected = ["profile"]
    for keywords, name, _ in tools.TOOL_KEYWORDS:
        if tools.tool_matches(name, keywords, question):
            selected.append(name)
    return selected


def _predict(case):
    state = {
        "employee_id": EMPLOYEE_ID,
        "own_name": OWN_NAME,
        "question": case["question"],
    }
    state.update(nodes.guard(state))
    state.update(nodes.classify(state))
    predicted_intent = state["intent"]
    if predicted_intent not in {"sensitive", "refuse"} and not nodes.has_question_content(
        state["masked_question"]
    ):
        predicted_intent = "no_question"
    predicted_tools = _selected_tools(predicted_intent, state["masked_question"])
    predicted_calc = _calc_prediction(state["masked_question"], state.get("classifier_calc"))
    return predicted_intent, predicted_tools, predicted_calc


def _metrics(cases, predictions, elapsed_ms):
    matrix = {label: {predicted: 0 for predicted in LABELS} for label in LABELS}
    for case, (predicted, _, _) in zip(cases, predictions):
        matrix[case["intent"]][predicted] += 1
    correct = sum(matrix[label][label] for label in LABELS)
    per_label = {}
    for label in LABELS:
        tp = matrix[label][label]
        predicted_count = sum(matrix[actual][label] for actual in LABELS)
        actual_count = sum(matrix[label].values())
        precision = tp / predicted_count if predicted_count else 0.0
        recall = tp / actual_count if actual_count else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_label[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "count": actual_count,
        }

    tool_cases = [
        (case, prediction)
        for case, prediction in zip(cases, predictions)
        if case["intent"] in {"lookup", "both"}
    ]
    tool_correct = sum(set(case["tools"]) == set(prediction[1]) for case, prediction in tool_cases)
    calc_cases = [(case, prediction) for case, prediction in zip(cases, predictions) if case["calc"]]
    calc_type_correct = sum(
        prediction[2] is not None and case["calc"]["type"] == prediction[2]["type"]
        for case, prediction in calc_cases
    )
    calc_value_correct = sum(
        prediction[2] is not None
        and case["calc"]["type"] == prediction[2]["type"]
        and case["calc"]["slots"] == prediction[2]["slots"]
        for case, prediction in calc_cases
    )
    confusions = []
    for actual in LABELS:
        for predicted in LABELS:
            count = matrix[actual][predicted]
            if actual != predicted and count:
                confusions.append((count, actual, predicted))
    confusions.sort(key=lambda row: (-row[0], row[1], row[2]))
    errors = [
        (case["id"], case["question"], case["intent"], prediction[0])
        for case, prediction in zip(cases, predictions)
        if case["intent"] != prediction[0]
    ]
    return {
        "accuracy": correct / len(cases) if cases else 0.0,
        "per_label": per_label,
        "matrix": matrix,
        "tool_accuracy": tool_correct / len(tool_cases) if tool_cases else None,
        "tool_correct": tool_correct,
        "tool_count": len(tool_cases),
        "calc_type_accuracy": calc_type_correct / len(calc_cases) if calc_cases else None,
        "calc_value_accuracy": calc_value_correct / len(calc_cases) if calc_cases else None,
        "calc_type_correct": calc_type_correct,
        "calc_value_correct": calc_value_correct,
        "calc_count": len(calc_cases),
        "confusions": confusions[:3],
        "errors": errors,
        "average_ms": sum(elapsed_ms) / len(elapsed_ms) if elapsed_ms else 0.0,
        "maximum_ms": max(elapsed_ms, default=0.0),
    }


def _markdown(split, cases, metrics):
    percent = lambda value: "N/A" if value is None else f"{value * 100:.1f}%"
    lines = [
        f"# 의도 분류 평가 ({split})",
        "",
        f"- 문장 수: {len(cases)}",
        f"- 전체 정확도: {percent(metrics['accuracy'])}",
        f"- 문장당 평균 처리 시간: {metrics['average_ms']:.3f} ms",
        "",
        "## 라벨별 지표",
        "",
        "| 라벨 | 정밀도 | 재현율 | F1 | 문장 수 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for label in LABELS:
        item = metrics["per_label"][label]
        lines.append(
            f"| {label} | {percent(item['precision'])} | {percent(item['recall'])} "
            f"| {percent(item['f1'])} | {item['count']} |"
        )
    lines.extend(["", "## 혼동 행렬", "", "| 정답 \\ 예측 | " + " | ".join(LABELS) + " |", "| --- | " + " | ".join("---:" for _ in LABELS) + " |"])
    for actual in LABELS:
        lines.append("| " + actual + " | " + " | ".join(str(metrics["matrix"][actual][p]) for p in LABELS) + " |")
    lines.extend([
        "",
        "## 도구·계산 일치율",
        "",
        f"- 조회 도구 (gold `lookup`·`both`): {metrics['tool_correct']}/{metrics['tool_count']} "
        f"({percent(metrics['tool_accuracy'])})",
        f"- 계산 종류: {metrics['calc_type_correct']}/{metrics['calc_count']} "
        f"({percent(metrics['calc_type_accuracy'])})",
        f"- 계산 슬롯 값 전체 일치: {metrics['calc_value_correct']}/{metrics['calc_count']} "
        f"({percent(metrics['calc_value_accuracy'])})",
        "",
        "## 가장 많은 혼동",
        "",
    ])
    if metrics["confusions"]:
        lines.extend(f"- {actual} → {predicted}: {count}건" for count, actual, predicted in metrics["confusions"])
    else:
        lines.append("- 오분류 없음")
    lines.extend(["", "## 오분류 문장", "", "| id | 문장 | 정답 | 예측 |", "| --- | --- | --- | --- |"])
    if metrics["errors"]:
        for case_id, question, expected, predicted in metrics["errors"]:
            safe_question = question.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {case_id} | {safe_question} | {expected} | {predicted} |")
    else:
        lines.append("| - | 오분류 없음 | - | - |")
    return "\n".join(lines) + "\n"


def load_extra_cases(path, exclude=()):
    """다른 질문 파일(처음 보는 질문 등). 한 줄에 {"id", "question", "intent", "tools", "calc"} 하나, ``` 줄은 건너뜀.
    exclude: 뺄 id (기존 평가셋과 거의 같은 문장 등)"""
    cases = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("```"):
            continue
        case = json.loads(s)
        if not {"id", "question", "intent", "tools", "calc"} <= set(case) or case["intent"] not in LABELS:
            raise ValueError(f"{path}:{n}: id·question·intent·tools·calc가 필요하고 intent는 {LABELS} 중 하나")
        calc = case["calc"]
        if isinstance(calc, dict) and "type" not in calc and len(calc) == 1:   # {"overtime": {...}} 모양도 받아요
            kind, slots = next(iter(calc.items()))
            case["calc"] = {"type": kind, "slots": slots}
        if case["id"] not in exclude:
            cases.append({**case, "split": case.get("split", "extra")})
    return cases


def _scoring_cases(split, exclude_examples=False, cases_path=None, exclude=()):
    if cases_path:                                    # 다른 질문 파일은 split 구분 없이 전부
        cases = load_extra_cases(cases_path, exclude)
    else:
        cases = _read_cases()
        cases = cases if split == "all" else [case for case in cases if case["split"] == split]
    if not exclude_examples:
        return cases
    examples = json.loads(EXAMPLES_PATH.read_text(encoding="utf-8")).get("examples", [])
    pairs = {e["id"].rsplit("-", 1)[0] for e in examples}
    return [case for case in cases if case["id"].rsplit("-", 1)[0] not in pairs]


def _run(cases, use_llm=False):
    """use_llm이면 분류 보조 LLM을 켜고(응답은 eval/intent/cache에 저장), 아니면 규칙만으로 채점해요."""
    if use_llm and config.USE_FAKE_LLM:
        raise SystemExit("--llm은 .env에 LLM_BASE_URL이 있어야 해요")
    original = config.CLASSIFY_LLM
    config.CLASSIFY_LLM = use_llm
    config.CALC_TOOLS = True
    calc_rules.load_rules.cache_clear()
    predictions = []
    elapsed_ms = []
    raw = {}                                   # 질문 → LLM이 고른 의도 (지금 방식은 sensitive·refuse만 쓰고 나머지는 버림)
    original_classify = llm_classify.classify

    def recording(question, today):
        result = original_classify(question, today)
        raw[question] = result["intent"] if result else None
        return result

    llm_classify.classify = recording
    try:
        with (llm_classify.evaluation_cache(CACHE_PATH) if use_llm else nullcontext(None)) as stats:
            for case in cases:
                started = perf_counter()
                predictions.append(_predict(case))
                elapsed_ms.append((perf_counter() - started) * 1000)
    finally:
        llm_classify.classify = original_classify
        calc_rules.load_rules.cache_clear()
        config.CLASSIFY_LLM = original
    metrics = _metrics(cases, predictions, elapsed_ms)
    if use_llm:
        metrics["llm"] = {"calls": stats.calls, "cache_hits": stats.cache_hits, "errors": len(stats.errors),
                          "json_failures": stats.json_failures, "validation_failures": stats.validation_failures}
        metrics["llm_raw"] = [raw.get(case["question"]) for case in cases]
    return predictions, metrics


# LLM 의도를 어디까지 쓸지 (같은 LLM 응답으로 비교, 호출은 늘지 않아요)
POLICIES = {
    "지금 (sensitive·refuse만)": lambda rule, llm: llm if llm in {"sensitive", "refuse"} else rule,
    "규칙이 rule·out_of_scope일 때만 LLM": lambda rule, llm: (llm if llm and (llm in {"sensitive", "refuse"}
                                                            or rule in {"rule", "out_of_scope"}) else rule),
    "전부 LLM (hybrid)": lambda rule, llm: llm or rule,
}


def policy_table(cases, rule_pred, llm_metrics):
    """LLM이 고른 의도를 쓰는 범위별 정확도. LLM을 안 부른 문장(규칙이 sensitive·refuse·질문 없음)은 규칙 결과 그대로."""
    raw = llm_metrics.get("llm_raw") or [None] * len(cases)
    out = []
    for name, pick in POLICIES.items():
        pred = [pick(r[0], l) for r, l in zip(rule_pred, raw)]
        ok = [p == c["intent"] for p, c in zip(pred, cases)]
        by = {}
        for label in LABELS:
            idx = [i for i, c in enumerate(cases) if c["intent"] == label]
            hit = sum(ok[i] for i in idx)
            fp = sum(1 for i, p in enumerate(pred) if p == label and cases[i]["intent"] != label)
            by[label] = (hit, len(idx), fp)
        fixed = sum(1 for p, r, c in zip(pred, rule_pred, cases) if p == c["intent"] != r[0])
        broke = sum(1 for p, r, c in zip(pred, rule_pred, cases) if r[0] == c["intent"] != p)
        out.append({"방식": name, "정확도": sum(ok) / len(cases), "고침": fixed, "새로 틀림": broke, "라벨별": by})
    return out


def compare(split="dev", cases_path=None, exclude=()):
    """규칙 vs 규칙+분류 보조 LLM을 같은 문장(few-shot 예시 쌍 제외)으로 채점해요. Colab 노트북도 이걸 불러요.

    반환: {"report": 마크다운, "cases", "rule": (예측, 지표), "llm": (예측, 지표), "changed": 바뀐 문장 목록}"""
    cases = _scoring_cases(split, exclude_examples=True, cases_path=cases_path, exclude=exclude)
    rule_pred, metrics = _run(cases)
    llm_pred, llm_metrics = _run(cases, use_llm=True)
    pct = lambda v: "N/A" if v is None else f"{v * 100:.1f}%"
    rows = [("전체 정확도", "accuracy"), ("조회 도구", "tool_accuracy"), ("계산 종류", "calc_type_accuracy"),
            ("계산 값", "calc_value_accuracy")]
    table = [f"# 규칙 vs 규칙+분류 보조 LLM ({split})", "", f"- 문장 수: {len(cases)} (few-shot 예시 쌍 제외)",
             f"- LLM: {llm_metrics['llm']}", "", "| 지표 | 규칙 | 규칙+LLM |", "| --- | ---: | ---: |"]
    table += [f"| {name} | {pct(metrics[key])} | {pct(llm_metrics[key])} |" for name, key in rows]
    for label in ("sensitive", "refuse"):
        for kind, word in (("recall", "재현율"), ("precision", "정밀도")):
            table.append(f"| {label} {word} | {pct(metrics['per_label'][label][kind])} | "
                         f"{pct(llm_metrics['per_label'][label][kind])} |")
    table.append(f"| 문장당 평균 ms | {metrics['average_ms']:.1f} | {llm_metrics['average_ms']:.1f} |")
    changed = []
    for case, r, l in zip(cases, rule_pred, llm_pred):
        if r[0] != l[0] or r[2] != l[2]:
            result = ("고침" if l[0] == case["intent"] and r[0] != case["intent"] else
                      "새로 틀림" if r[0] == case["intent"] and l[0] != case["intent"] else "그대로")
            changed.append({"id": case["id"], "question": case["question"], "정답": case["intent"],
                            "규칙": r[0], "규칙+LLM": l[0], "결과": result,
                            "정답 계산": case["calc"], "LLM 계산": l[2]})
    table += ["", "## 규칙+LLM에서 의도·계산이 바뀐 문장", "", "| id | 문장 | 정답 | 규칙 | 규칙+LLM | 의도 결과 |",
              "| --- | --- | --- | --- | --- | --- |"]
    table += [f"| {c['id']} | {c['question']} | {c['정답']} | {c['규칙']} | {c['규칙+LLM']} | {c['결과']} |" for c in changed]
    policies = policy_table(cases, rule_pred, llm_metrics)
    table += ["", "## LLM 의도를 어디까지 쓸지 (같은 LLM 응답)", "",
              "| 방식 | 정확도 | 규칙 대비 고침 | 새로 틀림 | " + " | ".join(LABELS) + " |",
              "| --- | ---: | ---: | ---: | " + " | ".join("---:" for _ in LABELS) + " |"]
    for p in policies:
        cells = [f"{h}/{n}" + (f" (오탐 {fp})" if fp else "") for h, n, fp in (p["라벨별"][label] for label in LABELS)]
        table.append(f"| {p['방식']} | {pct(p['정확도'])} | {p['고침']} | {p['새로 틀림']} | " + " | ".join(cells) + " |")
    table.append("")
    table.append("※ LLM 출력에는 no_question이 없어요 (질문 없음은 LLM 호출 전에 규칙이 판단). 그래서 질문 없음 오답은 어느 방식으로도 안 고쳐져요.")
    summary = "\n".join(table) + "\n"
    detail = _markdown(split + " · 규칙+LLM", cases, llm_metrics)
    return {"report": summary + "\n" + detail, "summary": summary, "detail": detail, "cases": cases,
            "rule": (rule_pred, metrics), "llm": (llm_pred, llm_metrics), "changed": changed, "policies": policies}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--llm", action="store_true", help="규칙 vs 규칙+분류 보조 LLM 비교")
    parser.add_argument("--cases", type=Path, help="다른 질문 파일(JSONL)로 채점 (예: 처음 보는 질문). --split은 무시")
    parser.add_argument("--exclude", default="", help="뺄 id, 쉼표로 구분")
    parser.add_argument("--out", type=Path, help="마크다운 결과 저장 경로")
    parser.add_argument("--apply-review", action="store_true", help="검수 CSV 수정 칸을 JSONL에 반영")
    args = parser.parse_args()

    exclude = tuple(x.strip() for x in args.exclude.split(",") if x.strip())
    if args.cases:
        args.split = "all"
    if args.split == "test" and not args.cases:
        print("경고: test 는 최종 비교 때 한 번만 재세요.", file=sys.stderr)
    if args.apply_review:
        _apply_review()
    if args.llm:
        report = compare(args.split, args.cases, exclude)["report"]
    else:
        cases = _scoring_cases(args.split, cases_path=args.cases, exclude=exclude)
        _, metrics = _run(cases)
        report = _markdown(args.cases.name if args.cases else args.split, cases, metrics)
    print(report, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8", newline="\n")
        print(f"\n저장: {args.out}")


if __name__ == "__main__":
    main()
