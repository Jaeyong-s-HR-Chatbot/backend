import json
from datetime import date
from pathlib import Path

import pytest

from app.calc_rules import (
    annual_leave,
    family_allowance,
    holiday_hours_limit,
    load_rules,
    overtime_pay,
)

RULES_PATH = Path(__file__).parent / "fixtures" / "rules_calc.json"
RULES = json.loads(RULES_PATH.read_text(encoding="utf-8"))


def _member(relation, age, gender="M", dependent=True):
    return {
        "relation": relation,
        "age": age,
        "gender": gender,
        "is_dependent": dependent,
    }


def test_annual_leave_first_year_boundaries_and_monthly_grants():
    assert annual_leave(date(2026, 1, 15), date(2026, 1, 15), RULES)["결과"]["부여일수"] == 0
    assert annual_leave(date(2026, 1, 15), date(2026, 4, 15), RULES)["결과"]["부여일수"] == 3
    assert annual_leave(date(2026, 1, 15), date(2026, 12, 15), RULES)["결과"]["부여일수"] == 11


def test_annual_leave_matches_generate_anniversary_year_formula():
    result = annual_leave(date(2025, 11, 15), date(2026, 11, 15), RULES)

    assert result["결과"]["부여일수"] == 15
    assert result["근거"] == RULES["annual_leave"]["_status"]


def test_annual_leave_matches_generate_service_increment_and_cap():
    granted = annual_leave(date(2023, 1, 1), date(2026, 11, 12), RULES)
    capped = annual_leave(date(2000, 1, 1), date(2030, 1, 1), RULES)

    assert granted["결과"]["부여일수"] == 16
    assert capped["결과"]["부여일수"] == 25


def test_annual_leave_rejects_reference_before_hire_and_incomplete_rules():
    assert annual_leave(date(2026, 2, 1), date(2026, 1, 31), RULES) is None
    assert annual_leave(date(2023, 1, 1), date(2026, 1, 1), {}) is None


def test_missing_rules_file_warns_and_skips(tmp_path, caplog):
    assert load_rules(str(tmp_path / "missing.json")) is None
    assert "계산 기준표를 읽을 수 없어 계산을 건너뜁니다" in caplog.text


def test_overtime_matches_generate_formula_and_rounding():
    result = overtime_pay(3_200_000, 10, 0, 0, RULES)

    assert result["결과"]["시간급"] == pytest.approx(3_200_000 / 209)
    assert result["결과"]["연장수당"] == 229_660
    assert result["결과"]["합계"] == 229_660


def test_overtime_adds_night_and_holiday_rates_as_generate_does():
    result = overtime_pay(3_200_000, 10, 4, 3, RULES)

    assert result["결과"]["연장수당"] == 229_660
    assert result["결과"]["야간가산"] == 30_620
    assert result["결과"]["휴일수당"] == 68_890
    assert result["결과"]["합계"] == 329_170


def test_overtime_holiday_limit_is_read_from_rule_status():
    limit = holiday_hours_limit(RULES)
    assert limit is not None

    at_limit = overtime_pay(3_200_000, 10, 0, limit, RULES)
    over_limit = overtime_pay(3_200_000, 10, 4, limit + 1, RULES)

    assert at_limit["결과"]["휴일수당"] > 0
    assert over_limit == {
        "입력 부족": f"휴일 {limit}시간 초과분의 배율이 기준표에 없음"
    }


def test_overtime_with_holiday_hours_requires_limit_in_rule_status():
    rules_without_limit = json.loads(json.dumps(RULES))
    rules_without_limit["overtime"]["_status"] = "근로기준법 제56조"

    assert overtime_pay(3_200_000, 10, 0, 1, rules_without_limit) is None


def test_overtime_accepts_zero_and_rejects_negative_or_missing_inputs():
    assert overtime_pay(3_200_000, 0, 0, 0, RULES)["결과"]["합계"] == 0
    assert overtime_pay(3_200_000, -1, 0, 0, RULES) is None
    assert overtime_pay(-1, 1, 0, 0, RULES) is None
    assert overtime_pay(3_200_000, 1, 0, 0, {}) is None


def test_family_allowance_matches_generate_spouse_children_and_parent_total():
    family = [
        _member("배우자", 35, "F"),
        _member("자녀", 12),
        _member("자녀", 8, "F"),
        _member("부", 60),
    ]

    result = family_allowance(4, family, RULES)

    assert result["결과"] == {
        "자녀수당": 130_000,
        "배우자수당": 30_000,
        "직계존속수당": 20_000,
        "월 가족수당": 180_000,
    }


@pytest.mark.parametrize(
    ("family", "expected"),
    [
        ([_member("배우자", 40, "F")], 30_000),
        ([_member("자녀", 5)], 50_000),
        ([_member("자녀", 5), _member("자녀", 10, "F")], 130_000),
        ([_member("자녀", 5), _member("자녀", 10, "F"), _member("자녀", 15)], 250_000),
        ([_member("자녀", 18)], 50_000),
        ([_member("자녀", 19)], 0),
    ],
)
def test_family_allowance_spouse_children_order_and_age_boundary(family, expected):
    assert family_allowance(4, family, RULES)["결과"]["월 가족수당"] == expected


@pytest.mark.parametrize(
    ("family", "expected"),
    [
        ([_member("부", 59)], 0),
        ([_member("부", 60)], 20_000),
        ([_member("모", 54, "F")], 0),
        ([_member("모", 55, "F")], 20_000),
        ([_member("직계존속", 60, "M")], 20_000),
        ([_member("직계존속", 55, "F")], 20_000),
    ],
)
def test_family_allowance_parent_gender_and_age_boundaries(family, expected):
    assert family_allowance(4, family, RULES)["결과"]["월 가족수당"] == expected


def test_family_allowance_caps_non_children_and_senior_grades_get_children_only():
    spouse_and_parents = [_member("배우자", 40, "F")] + [
        _member("부" if i % 2 == 0 else "모", 60 if i % 2 == 0 else 55, "M" if i % 2 == 0 else "F")
        for i in range(5)
    ]
    assert family_allowance(4, spouse_and_parents, RULES)["결과"]["월 가족수당"] == 90_000

    senior_family = [_member("배우자", 40, "F"), _member("자녀", 10)]
    assert family_allowance(2, senior_family, RULES)["결과"]["월 가족수당"] == 50_000


def test_family_allowance_ignores_non_dependent_and_rejects_invalid_input():
    assert family_allowance(4, [_member("배우자", 40, "F", False)], RULES)["결과"]["월 가족수당"] == 0
    assert family_allowance(4, [_member("자녀", -1)], RULES) is None
    assert family_allowance(99, [], RULES) is None
