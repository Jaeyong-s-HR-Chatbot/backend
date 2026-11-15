"""Pure calculations driven by the HR rules JSON."""
import json
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from functools import lru_cache

log = logging.getLogger("hr_chat")


@lru_cache(maxsize=8)
def load_rules(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as file:
            rules = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        log.warning("계산 기준표를 읽을 수 없어 계산을 건너뜁니다: %s", type(exc).__name__)
        return None
    return rules if isinstance(rules, dict) else None


def _policy(rules: dict, key: str, required: tuple[str, ...]) -> dict | None:
    if not isinstance(rules, dict):
        return None
    policy = rules.get(key)
    if not isinstance(policy, dict) or not isinstance(policy.get("_status"), str) or not policy["_status"].strip():
        return None
    if any(field not in policy for field in required):
        return None
    return policy


def _date(value) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _number(value) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _display(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


def annual_leave(hire_date, reference_date, rules: dict) -> dict | None:
    policy = _policy(
        rules,
        "annual_leave",
        ("first_year_monthly", "first_year_max", "base", "add_every_years", "max"),
    )
    hire = _date(hire_date)
    asof = _date(reference_date)
    if not policy or not hire or not asof or asof < hire:
        return None
    monthly = _number(policy["first_year_monthly"])
    first_max = _number(policy["first_year_max"])
    base = _number(policy["base"])
    add_every = _number(policy["add_every_years"])
    maximum = _number(policy["max"])
    if (
        any(value is None for value in (monthly, first_max, base, add_every, maximum))
        or min(monthly, first_max, base, add_every, maximum) < 0
        or add_every == 0
        or add_every != add_every.to_integral_value()
    ):
        return None

    if hire.year == asof.year:
        months, current = 0, hire
        while True:
            year = current.year + (current.month == 12)
            month = current.month % 12 + 1
            current = date(year, month, min(hire.day, 28))
            if current > asof or current.year > asof.year:
                break
            months += 1
        granted = min(first_max, monthly * months)
        formula = f"min({first_max}, {monthly}×{months})={granted}일"
    else:
        years = asof.year - hire.year
        granted = min(maximum, base + Decimal((years - 1) // int(add_every)))
        formula = f"min({maximum}, {base}+({years}-1)÷{add_every}의 몫)={granted}일"

    result_value = int(granted) if granted == granted.to_integral_value() else float(granted)
    return {
        "종류": "연차 일수",
        "입력": {"입사일": hire.isoformat(), "기준일": asof.isoformat()},
        "결과": {"부여일수": result_value},
        "계산식": formula,
        "근거": policy["_status"],
    }


def _round_down_ten(value: Decimal) -> int:
    return int((value / Decimal(10)).to_integral_value(rounding=ROUND_DOWN) * 10)


def holiday_hours_limit(rules: dict) -> Decimal | None:
    policy = _policy(rules, "overtime", ("holiday_rate",))
    if not policy:
        return None
    match = re.search(r"휴일\s*(\d+(?:\.\d+)?)\s*시간\s*이내", policy["_status"])
    return _number(match.group(1)) if match else None


def holiday_hours_excess(holiday_hours, rules: dict) -> dict | None:
    hours = _number(holiday_hours)
    limit = holiday_hours_limit(rules)
    if hours is None or limit is None or hours <= limit:
        return None
    return {"입력 부족": f"휴일 {_display(limit)}시간 초과분의 배율이 기준표에 없음"}


def overtime_pay(
    ordinary_wage,
    overtime_hours,
    night_hours,
    holiday_hours,
    rules: dict,
) -> dict | None:
    policy = _policy(
        rules,
        "overtime",
        ("monthly_hours", "overtime_rate", "night_extra_rate", "holiday_rate"),
    )
    wage = _number(ordinary_wage)
    overtime = _number(overtime_hours)
    night = _number(night_hours)
    holiday = _number(holiday_hours)
    if (
        not policy
        or any(value is None for value in (overtime, night, holiday))
        or min(overtime, night, holiday) < 0
    ):
        return None
    if holiday > 0 and holiday_hours_limit(rules) is None:
        return None
    excess = holiday_hours_excess(holiday, rules)
    if excess is not None:
        return excess
    if wage is None or wage < 0:
        return None
    monthly_hours = _number(policy["monthly_hours"])
    overtime_rate = _number(policy["overtime_rate"])
    night_rate = _number(policy["night_extra_rate"])
    holiday_rate = _number(policy["holiday_rate"])
    if (
        any(value is None for value in (monthly_hours, overtime_rate, night_rate, holiday_rate))
        or monthly_hours <= 0
        or min(overtime_rate, night_rate, holiday_rate) < 0
    ):
        return None

    hourly = wage / monthly_hours
    overtime_amount = _round_down_ten(hourly * overtime * overtime_rate)
    night_amount = _round_down_ten(hourly * night * night_rate)
    holiday_amount = _round_down_ten(hourly * holiday * holiday_rate)
    total = overtime_amount + night_amount + holiday_amount
    formula = (
        f"시간급={_display(wage)}÷{_display(monthly_hours)}; "
        f"연장={_display(hourly)}×{_display(overtime)}×{_display(overtime_rate)}={overtime_amount}원; "
        f"야간={_display(hourly)}×{_display(night)}×{_display(night_rate)}={night_amount}원; "
        f"휴일={_display(hourly)}×{_display(holiday)}×{_display(holiday_rate)}={holiday_amount}원"
    )
    return {
        "종류": "시간외수당",
        "입력": {
            "통상임금": int(wage) if wage == wage.to_integral_value() else float(wage),
            "연장시간": float(overtime),
            "야간시간": float(night),
            "휴일시간": float(holiday),
        },
        "결과": {
            "시간급": float(hourly),
            "연장수당": overtime_amount,
            "야간가산": night_amount,
            "휴일수당": holiday_amount,
            "합계": total,
        },
        "계산식": formula,
        "근거": policy["_status"],
    }


def family_allowance(grade, family: list[dict], rules: dict) -> dict | None:
    if not isinstance(rules, dict):
        return None
    policy = _policy(
        rules,
        "family_allowance",
        (
            "배우자",
            "자녀",
            "직계존속",
            "child_age_limit",
            "parent_age_min_male",
            "parent_age_min_female",
            "non_child_max",
            "children_only_from_grade",
        ),
    )
    grades = rules.get("grades")
    if not policy or not isinstance(grades, dict) or not isinstance(family, list):
        return None
    try:
        grade_value = _number(grade)
    except (TypeError, ValueError):
        return None
    if grade_value is None or grade_value != grade_value.to_integral_value():
        return None
    grade_number = int(grade_value)
    if str(grade_number) not in grades or not isinstance(policy["자녀"], list) or not policy["자녀"]:
        return None
    child_rates = [_number(value) for value in policy["자녀"]]
    spouse_rate = _number(policy["배우자"])
    parent_rate = _number(policy["직계존속"])
    child_age_limit = _number(policy["child_age_limit"])
    male_age_min = _number(policy["parent_age_min_male"])
    female_age_min = _number(policy["parent_age_min_female"])
    non_child_max = _number(policy["non_child_max"])
    children_only_grade = _number(policy["children_only_from_grade"])
    values = child_rates + [
        spouse_rate,
        parent_rate,
        child_age_limit,
        male_age_min,
        female_age_min,
        non_child_max,
        children_only_grade,
    ]
    if any(value is None or value < 0 for value in values):
        return None

    spouse_count = 0
    children = []
    parents = 0
    for member in family:
        if not isinstance(member, dict):
            return None
        relation = member.get("관계", member.get("relation"))
        age = _number(member.get("나이", member.get("age")))
        gender = member.get("성별", member.get("gender"))
        dependent = member.get("부양", member.get("is_dependent"))
        if (
            not isinstance(relation, str)
            or age is None
            or age < 0
            or age != age.to_integral_value()
            or not isinstance(gender, str)
            or gender not in {"M", "F", "남", "여"}
            or not isinstance(dependent, (bool, str))
            or dependent not in {True, False, "true", "false", "True", "False"}
        ):
            return None
        if relation in {"부", "배우자부"} and gender not in {"M", "남"}:
            return None
        if relation in {"모", "배우자모"} and gender not in {"F", "여"}:
            return None
        if dependent not in (True, "true", "True"):
            continue
        if relation == "배우자":
            spouse_count = 1
        elif relation == "자녀" and age < child_age_limit:
            children.append(age)
        elif relation in {"부", "배우자부"} and age >= male_age_min:
            parents += 1
        elif relation in {"모", "배우자모"} and age >= female_age_min:
            parents += 1
        elif relation == "직계존속":
            if gender in {"M", "남"} and age >= male_age_min:
                parents += 1
            elif gender in {"F", "여"} and age >= female_age_min:
                parents += 1

    children.sort(reverse=True)
    child_amount = sum(
        (child_rates[min(index, len(child_rates) - 1)] for index in range(len(children))),
        Decimal(0),
    )
    children_only = Decimal(grade_number) <= children_only_grade
    paid_parents = 0 if children_only else max(
        0, min(spouse_count + parents, int(non_child_max)) - spouse_count
    )
    spouse_amount = Decimal(0) if children_only else spouse_rate * spouse_count
    parent_amount = parent_rate * paid_parents
    total = child_amount + spouse_amount + parent_amount
    return {
        "종류": "가족수당",
        "입력": {"직급": grade_number, "가족": family},
        "결과": {
            "자녀수당": int(child_amount),
            "배우자수당": int(spouse_amount),
            "직계존속수당": int(parent_amount),
            "월 가족수당": int(total),
        },
        "계산식": (
            f"자녀 {len(children)}명 {int(child_amount)}원 + 배우자 {spouse_count if not children_only else 0}명 "
            f"{int(spouse_amount)}원 + 직계존속 {paid_parents}명 {int(parent_amount)}원 = {int(total)}원"
        ),
        "근거": policy["_status"],
    }
