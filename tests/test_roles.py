import logging
import sqlite3
from datetime import date, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app import config, db, roles
from app.fake_data import FAKE_PASSWORD
from app.main import app
from app.masking import pseudonymize


EXPECTED = [
    ("E1422", {"EMPLOYEE"}),
    ("E1243", {"EMPLOYEE", "TEAM_LEAD"}),
    ("E1686", {"EMPLOYEE", "HR_DISCIPLINE"}),
    ("E1707", {"EMPLOYEE", "HR_LABOR"}),
    ("E1545", {"EMPLOYEE"}),
    ("E1622", {"EMPLOYEE", "SYS_ADMIN"}),
    ("E1007", {"EMPLOYEE", "TEAM_LEAD"}),
    ("E1459", {"EMPLOYEE"}),
]


@pytest.fixture
def sqlite_roles(sqlite_hr_db, monkeypatch):
    con = sqlite_hr_db
    schemas = {row[1] for row in con.execute("PRAGMA database_list")}
    if "app" not in schemas:
        con.execute("ATTACH DATABASE ':memory:' AS app")
    con.execute(
        "CREATE TABLE app.user_roles (emp_id TEXT, role TEXT, scope_type TEXT, "
        "scope_value TEXT, valid_from DATE, valid_to DATE)"
    )
    con.executemany(
        "INSERT INTO app.user_roles VALUES (?, ?, 'GLOBAL', '*', ?, NULL)",
        [("E1686", "HR_DISCIPLINE", "2000-01-01"),
         ("E1707", "HR_LABOR", "2000-01-01"),
         ("E1622", "SYS_ADMIN", "2000-01-01")],
    )
    monkeypatch.setattr(config, "USE_DB", True)
    yield con
    con.execute("DROP TABLE IF EXISTS app.user_roles")


@pytest.mark.parametrize(("emp_id", "expected"), EXPECTED)
def test_fake_demo_roles(emp_id, expected, monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 20)

    monkeypatch.setattr(config, "USE_DB", False)
    monkeypatch.setattr(roles, "date", Today)
    assert roles.get_roles(emp_id) == expected


@pytest.mark.parametrize(("emp_id", "expected"), EXPECTED)
def test_sqlite_demo_roles(emp_id, expected, sqlite_roles):
    assert roles.get_roles(emp_id) == expected


def test_expired_future_and_non_global_grants_are_ignored(sqlite_roles):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    sqlite_roles.execute("UPDATE app.user_roles SET valid_to = ? WHERE emp_id = ?", (yesterday, "E1686"))
    sqlite_roles.execute("UPDATE app.user_roles SET valid_from = ? WHERE emp_id = ?", (tomorrow, "E1707"))
    sqlite_roles.execute("UPDATE app.user_roles SET scope_type = 'DEPT' WHERE emp_id = ?", ("E1622",))
    for emp_id in ("E1686", "E1707", "E1622"):
        assert roles.get_roles(emp_id) == {"EMPLOYEE"}


def test_missing_table_fails_closed_even_for_team_lead(sqlite_roles, caplog):
    sqlite_roles.execute("DROP TABLE app.user_roles")
    with caplog.at_level(logging.WARNING, logger="app.roles"):
        assert roles.get_roles("E1243") == {"EMPLOYEE"}
    assert pseudonymize("E1243") in caplog.text
    assert "E1243" not in caplog.text


def test_database_error_fails_closed(monkeypatch, caplog):
    monkeypatch.setattr(config, "USE_DB", True)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("sensitive connection details")

    monkeypatch.setattr(db, "fetch_all", fail)
    with caplog.at_level(logging.WARNING, logger="app.roles"):
        assert roles.get_roles("E1686") == {"EMPLOYEE"}
    assert "E1686" not in caplog.text
    assert "sensitive connection details" not in caplog.text


def test_clearance_is_display_only():
    assert roles.clearance({"EMPLOYEE", "HR_DISCIPLINE"}) == 4
    assert roles.clearance({"EMPLOYEE", "HR_LABOR"}) == 4
    assert roles.clearance({"EMPLOYEE", "SYS_ADMIN", "TEAM_LEAD"}) == 2


def test_me_ignores_client_roles_and_token_claims(monkeypatch):
    monkeypatch.setattr(config, "USE_DB", False)
    client = TestClient(app)
    login = client.post("/auth/login", json={"username": "E1584", "password": FAKE_PASSWORD})
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    original = client.get("/me", headers=headers).json()
    supplied = client.get("/me?role=HR_DISCIPLINE&conf_roles=HR_LABOR", headers=headers).json()
    payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    payload["conf_roles"] = ["HR_DISCIPLINE"]
    altered_token = jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")
    altered = client.get("/me", headers={"Authorization": f"Bearer {altered_token}"}).json()
    assert supplied == original == altered
    assert original["conf_roles"] == ["EMPLOYEE"]
    assert original["clearance"] == 2
    assert {"gender", "birth_date", "email", "base_salary"}.isdisjoint(original)
