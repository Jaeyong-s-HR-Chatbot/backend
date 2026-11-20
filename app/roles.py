"""사번으로 기밀문서 역할을 조회해요."""
import logging
import sqlite3
from datetime import date

import psycopg

from . import config, db
from .fake_data import CONF_ROLE_GRANTS, CONF_TEAM_LEADS
from .masking import pseudonymize

log = logging.getLogger(__name__)
_GRANT_ROLES = {"HR_DISCIPLINE", "HR_LABOR", "SYS_ADMIN"}


def get_roles(emp_id: str) -> set[str]:
    result = {"EMPLOYEE"}
    if not config.USE_DB:
        if emp_id in CONF_TEAM_LEADS:
            result.add("TEAM_LEAD")
        grant = CONF_ROLE_GRANTS.get(emp_id)
        today = date.today().isoformat()
        if grant and grant["valid_from"] <= today and (
            grant["valid_to"] is None or today <= grant["valid_to"]
        ):
            result.add(grant["role"])
        return result

    try:
        rows = db.fetch_all(
            "SELECT e.duty, e.status, r.role FROM hr.employees e "
            "LEFT JOIN app.user_roles r ON r.emp_id = e.emp_id "
            "AND r.scope_type = 'GLOBAL' AND r.scope_value = '*' "
            "AND r.valid_from <= CURRENT_DATE "
            "AND (r.valid_to IS NULL OR r.valid_to >= CURRENT_DATE) "
            "WHERE e.emp_id = %s",
            (emp_id,),
        )
    except (psycopg.Error, sqlite3.Error, OSError) as exc:
        log.warning("기밀문서 역할 조회 실패: %s (%s)", pseudonymize(emp_id), type(exc).__name__)
        return result
    for row in rows:
        if row["duty"] == "팀장" and row["status"] == "재직":
            result.add("TEAM_LEAD")
        if row["role"] in _GRANT_ROLES:
            result.add(row["role"])
    return result


def clearance(roles: set[str]) -> int:
    """화면 표시용 등급이며, 문서 접근 판정에 사용하지 않아요."""
    return 4 if roles & {"HR_DISCIPLINE", "HR_LABOR"} else 2


# SUBJECT, COMMITTEE, COUNCIL은 이후 문서 머리말로 판단해요.
