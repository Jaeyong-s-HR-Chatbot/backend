"""공용 픽스처: DB 저장소의 가짜 HR CSV(../DB/data/hr, 출퇴근은 ../DB/data/extra)를 메모리 SQLite에 올려 db.fetch_all을 바꿔 끼움.

Postgres 없이 인사팀 집계 SQL을 돌려 보기 위한 것. SQL은 Postgres와 SQLite 양쪽에서 도는 문법만 써요.
"""
import csv
import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest

HR_CSV = Path(__file__).resolve().parents[2] / "DB" / "data" / "hr"
HR_TABLES = ["employees", "departments", "leave_balance", "leave_history", "requests", "absence_history",
             "hr_history", "training", "payroll", "welfare_points", "family", "business_trips", "audit_log"]
_INT = re.compile(r"-?\d+")
_FLOAT = re.compile(r"-?\d+\.\d+")


def _val(v: str):
    if v == "":
        return None
    if _INT.fullmatch(v) and not (len(v) > 1 and v.startswith("0")):
        return int(v)
    if _FLOAT.fullmatch(v):
        return float(v)
    return v


@pytest.fixture(scope="session")
def hr_sqlite():
    if not (HR_CSV / "employees.csv").is_file():
        pytest.skip("DB/data/hr CSV가 없어 건너뜀")
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.execute("ATTACH DATABASE ':memory:' AS hr")
    for t in HR_TABLES + ["attendance"]:
        path = HR_CSV / f"{t}.csv" if t != "attendance" else HR_CSV.parent / "extra" / "attendance.csv"
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        cols = rows[0]
        con.execute(f"CREATE TABLE hr.{t} ({', '.join(cols)})")
        con.executemany(f"INSERT INTO hr.{t} VALUES ({', '.join('?' * len(cols))})", [[_val(v) for v in r] for r in rows[1:]])
    con.row_factory = sqlite3.Row
    yield con
    con.close()


@pytest.fixture
def sqlite_hr_db(hr_sqlite, monkeypatch):
    """app.db.fetch_all → 메모리 SQLite (%s → ?, 날짜 → ISO 문자열)"""
    from app import db

    def fetch_all(sql, params=()):
        params = tuple(p.isoformat() if isinstance(p, date) else p for p in params)
        return [dict(r) for r in hr_sqlite.execute(sql.replace("%s", "?"), params).fetchall()]
    monkeypatch.setattr(db, "fetch_all", fetch_all)
    return hr_sqlite


@pytest.fixture(autouse=True)
def no_classify_llm(monkeypatch):
    """테스트는 기본으로 분류 보조 LLM을 끄고 규칙만 써요 (.env에 실제 LLM 주소가 있어도 네트워크로 안 나감).
    분류 LLM을 시험하는 테스트는 config.CLASSIFY_LLM·USE_FAKE_LLM을 직접 켜고 요청 함수를 가짜로 바꿔요."""
    from app import config
    monkeypatch.setattr(config, "CLASSIFY_LLM", False)
