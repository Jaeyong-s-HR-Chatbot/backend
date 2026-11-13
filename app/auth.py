"""로그인과 토큰.

- HRDB_URL이 있으면: hr.users의 로그인 ID와 비밀번호 해시(pbkdf2-sha256)를 확인해요.
- 없으면: 가짜 계정(fake_data.USERS)으로 동작해요.
토큰 안의 사번(sub)이 파이프라인의 employee_id로 들어가요. 프론트는 사번을 따로 보내지 않아요.
"""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import config, db
from .fake_data import FAKE_PASSWORD, USERS
from .schemas import Me

_bearer = HTTPBearer(auto_error=False)


def _check_pbkdf2(password: str, stored: str) -> bool:
    """저장 형식: pbkdf2$<salt>$<hex>  (sha256, 100,000회, 규태님 generate.py와 같은 방식)"""
    try:
        _, salt, want = stored.split("$")
    except ValueError:
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return hmac.compare_digest(got, want)


def _me_from_db(emp_id: str) -> dict | None:
    r = db.fetch_one("SELECT e.emp_id, e.name, d.dept_name, e.position, e.grade, u.role FROM hr.employees e "
                     "JOIN hr.users u USING (emp_id) LEFT JOIN hr.departments d USING (dept_id) "
                     "WHERE e.emp_id = %s AND u.is_active", (emp_id,))
    if not r:
        return None
    return {"employee_id": r["emp_id"], "name": r["name"], "dept": r["dept_name"] or "", "grade": f"{r['position']}({r['grade']}급)",
            "role": r["role"] if r["role"] in ("employee", "hr_admin") else "employee"}


def authenticate(username: str, password: str) -> dict | None:
    username = username.strip()
    if config.USE_DB:
        u = db.fetch_one("SELECT emp_id, password_hash FROM hr.users WHERE is_active AND (lower(login_id) = lower(%s) OR emp_id = upper(%s))",
                         (username, username))
        if u and _check_pbkdf2(password, u["password_hash"]):
            return _me_from_db(u["emp_id"])
        return None
    user = USERS.get(username.upper())
    if user and hmac.compare_digest(password, FAKE_PASSWORD):
        return user
    return None


def create_token(employee_id: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": employee_id, "role": role, "iat": now, "exp": now + timedelta(minutes=config.JWT_EXPIRE_MIN)}
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def current_user(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Me:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "로그인이 필요해요.", headers={"WWW-Authenticate": "Bearer"})
    if cred is None:
        raise unauthorized
    try:
        payload = jwt.decode(cred.credentials, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise unauthorized
    sub = payload.get("sub", "")
    user = _me_from_db(sub) if config.USE_DB else USERS.get(sub)
    if not user:
        raise unauthorized
    return Me(**user)
