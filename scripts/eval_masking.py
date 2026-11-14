"""Measure runtime text masking against synthetic cases."""
import argparse
import importlib.util
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "masking_cases", ROOT / "tests" / "masking_cases.py"
)
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)

def load_masker(revision=None):
    if revision is None:
        from app.masking import mask_text

        return mask_text
    source = subprocess.run(
        ["git", "show", f"{revision}:app/masking.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    module = types.ModuleType("app._masking_eval_baseline")
    module.__package__ = "app"
    sys.modules[module.__name__] = module
    exec(compile(source, f"{revision}:app/masking.py", "exec"), module.__dict__)
    return module.mask_text


def result_for(sentence, mask_text):
    return mask_text(
        sentence,
        own_emp_id=cases.OWN_EMP_ID,
        own_name=cases.OWN_NAME,
    )


def measure(mask_text):
    rows = []
    for sentence, expected, description in cases.MASK_CASES:
        result = result_for(sentence, mask_text)
        actual = result.found
        for kind, count in expected.items():
            missing = max(0, count - actual.get(kind, 0))
            extra_other = sum(
                max(0, actual_count - expected.get(other_kind, 0))
                for other_kind, actual_count in actual.items()
                if other_kind != kind
            )
            wrong = min(missing, extra_other)
            rows.append({
                "kind": kind,
                "expected": count,
                "correct": min(count, actual.get(kind, 0)),
                "missed": missing - wrong,
                "wrong": wrong,
                "sentence": sentence,
                "description": description,
                "actual": actual,
            })

    safe_rows = []
    for sentence, description in cases.NO_MASK_CASES:
        result = result_for(sentence, mask_text)
        if result.found:
            safe_rows.append({
                "sentence": sentence,
                "description": description,
                "actual": result.found,
            })
    return rows, safe_rows


def render(rows, safe_rows, phase):
    kinds = []
    for row in rows:
        if row["kind"] not in kinds:
            kinds.append(row["kind"])

    lines = [
        f"## {phase}",
        "",
        "| 대상 | 사례 수 | 맞게 가림 | 놓침 | 종류를 틀림 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for kind in kinds:
        target = [row for row in rows if row["kind"] == kind]
        lines.append(
            f"| [{kind}] | {len(target)} | "
            f"{sum(row['correct'] for row in target)} | "
            f"{sum(row['missed'] for row in target)} | "
            f"{sum(row['wrong'] for row in target)} |"
        )
    lines.extend([
        "",
        f"가리면 안 되는 사례: {len(cases.NO_MASK_CASES)}개, "
        f"잘못 가림 {len(safe_rows)}개.",
        "",
        "### 놓친 사례 및 종류를 틀린 사례",
        "",
    ])
    problems = [row for row in rows if row["missed"] or row["wrong"]]
    if problems:
        lines.extend([
            "| 입력 | 기대 | 실제 | 설명 |",
            "| --- | --- | --- | --- |",
        ])
        for row in problems:
            expected = f"[{row['kind']}] × {row['expected']}"
            actual = ", ".join(
                f"[{kind}] × {count}" for kind, count in row["actual"].items()
            ) or "마스킹 없음"
            status = "놓침" if row["missed"] else "종류 오분류"
            lines.append(
                f"| `{row['sentence']}` | {expected} ({status}) | "
                f"{actual} | {row['description']} |"
            )
    else:
        lines.append("없음.")
    lines.extend([
        "",
        "### 잘못 가린 사례",
        "",
    ])
    if safe_rows:
        lines.extend([
            "| 입력 | 기대 | 실제 | 설명 |",
            "| --- | --- | --- | --- |",
        ])
        for row in safe_rows:
            actual = ", ".join(
                f"[{kind}] × {count}" for kind, count in row["actual"].items()
            )
            lines.append(
                f"| `{row['sentence']}` | 마스킹 없음 | {actual} | "
                f"{row['description']} |"
            )
    else:
        lines.append("없음.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("수정 전", "수정 후"), default="수정 전")
    parser.add_argument("--revision", help="measure a committed masking.py version")
    args = parser.parse_args()
    rows, safe_rows = measure(load_masker(args.revision))
    report = render(rows, safe_rows, args.phase)
    print(report)

    docs = ROOT / "docs" / "masking_eval.md"
    docs.parent.mkdir(parents=True, exist_ok=True)
    if args.phase == "수정 전" or not docs.exists():
        docs.write_text(
            "# 마스킹 측정 결과\n\n"
            "측정 방법: `tests/masking_cases.py`의 가짜 문장에 "
            "`mask_text()`를 적용하고 기대 태그 개수를 비교합니다. "
            "본인 사번은 `e1584`, 본인 이름은 가상 문자열 `가상직원`으로 "
            "고정합니다. 사례 수는 문장 수이며 맞게 가림·놓침·종류 오분류는 "
            "기대 태그 발생 건수입니다. golden set은 저장소에 없어 포함하지 "
            "않았습니다. `RUN_LOCAL.md`의 E1412 타인 조회 질문은 명시된 "
            "타인 사번 규칙에 따라 마스킹 대상 예외로 측정했습니다.\n\n"
            + report + "\n",
            encoding="utf-8",
        )
    else:
        previous = docs.read_text(encoding="utf-8")
        marker = "## 수정 후"
        previous = previous.split(marker, 1)[0].rstrip()
        docs.write_text(previous + "\n\n" + report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
