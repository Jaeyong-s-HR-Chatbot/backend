"""누리철도㈜ HR 챗봇 마스킹 모듈 (기본판: 2026-11-09 작성, DB 가짜 데이터로 검증)
- 마스킹 작업(ETL): 컬럼 값을 화면용 형식으로 가림  → mask_rrn, mask_account ...
- 마스킹 로직(런타임): 자유 텍스트에서 개인정보를 찾아 가림 → mask_text
두 쪽이 이 파일 하나를 함께 써서 형식이 항상 같다. 기준: masking_rules.md
"""
import calendar
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 1) 컬럼 값 마스킹 (화면 표시용)

def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def mask_rrn(v: str) -> str:
    """주민번호: 생년월일 6자리 + 성별 1자리만 노출  900501-1******"""
    d = _digits(v)
    return f"{d[:6]}-{d[6:7]}******" if len(d) == 13 else "******-*******"


def mask_account(v: str) -> str:
    """계좌번호: 첫 묶음 + 마지막 3자리만 노출  999-**-***456"""
    groups = re.split(r"[-\s]", v.strip()) if v else []
    if len(groups) >= 2:
        out = [groups[0]] + ["*" * len(g) for g in groups[1:-1]]
        last = groups[-1]
        out.append("*" * max(len(last) - 3, 0) + last[-3:])
        return "-".join(out)
    d = _digits(v)
    return d[:3] + "*" * max(len(d) - 6, 0) + d[-3:] if len(d) > 6 else "*" * len(d)


def mask_card(v: str) -> str:
    """카드번호: 7~12번째 자리 가림 (업계 기준)  1234-56**-****-3456"""
    d = _digits(v)
    if len(d) < 15:
        return "*" * len(d)
    m = d[:6] + "*" * 6 + d[12:]
    return "-".join(m[i:i + 4] for i in range(0, len(m), 4))


def mask_phone(v: str) -> str:
    """전화번호: 가운데 국번 가림  010-****-1234"""
    d = _digits(v)
    if len(d) == 11:
        return f"{d[:3]}-****-{d[7:]}"
    if len(d) in (9, 10):
        head = 2 if d.startswith("02") else 3
        mid = len(d) - head - 4
        return f"{d[:head]}-{'*' * mid}-{d[-4:]}"
    return "***-****-****"


def mask_email(v: str) -> str:
    """이메일: 아이디 첫 글자만 노출  k**@nuri.test"""
    if not v or "@" not in v:
        return "***"
    user, dom = v.split("@", 1)
    return f"{user[:1]}{'*' * max(len(user) - 1, 2)}@{dom}"


def mask_name(v: str) -> str:
    """이름: 성만 노출  김○○"""
    return (v[:1] + "○" * (len(v) - 1)) if v else ""


def mask_emp_id(v: str) -> str:
    """사번: 앞 3자리만 노출  E10**"""
    return v[:3] + "*" * (len(v) - 3) if v else ""


def mask_address(sido: str, sigungu: str, detail: str = "") -> str:
    """주소: 시·도 + 시·군·구까지만 노출  경기도 화성시 ***"""
    return f"{sido} {sigungu} ***".strip()


# ---------------------------------------------------------------- 2) 자유 텍스트 마스킹 (질문·답변·로그용)

@dataclass
class MaskResult:
    text: str
    found: dict[str, int] = field(default_factory=dict)


_RRN = re.compile(r"(?<!\d)(\d{6})(?:\s*[-.]\s*|\s*)([1-8]\d{6})(?!\d)")
_CARD = re.compile(r"(?<!\d)\d(?:[-\s]?\d){14,15}(?!\d)")
_PHONE_SEPARATOR = r"(?:\s*[-.]\s*|\s*)"
_PHONE = re.compile(
    r"(?<![\dA-Za-z])"
    r"(?:\+82" + _PHONE_SEPARATOR + r"(?:10|2|[3-6][1-5])|"
    r"\(?(?:01[016789]|02|0[3-6][1-5])\)?)"
    + _PHONE_SEPARATOR + r"\d{3,4}" + _PHONE_SEPARATOR + r"\d{4}(?!\d)(?![-.]\d)"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_DASHED_ACCOUNT = re.compile(r"(?<![\d-])\d{2,6}-\d{2,6}-\d{2,7}(?![\d-])")
_PLAIN_ACCOUNT = re.compile(r"(?<!\d)\d{10,14}(?!\d)")
_ACCOUNT_CONTEXT = re.compile(
    r"계좌|통장|입금|국민(?:은행)?|신한은행|우리은행|하나은행|농협(?:은행)?|"
    r"기업은행|카카오뱅크|토스뱅크|케이뱅크|우체국|새마을금고|SC제일은행|"
    r"산업은행|수협은행|씨티은행"
)
_EMP_ID = re.compile(r"(?<![A-Za-z0-9])[Ee]\d{4}(?![A-Za-z0-9])")
_PHONE_SHAPED_ACCOUNT = re.compile(r"^(?:\d{2}|\d{3})-\d{3,4}-\d{4}$")


def _valid_rrn_date(value: str) -> bool:
    year, month, day = int(value[:2]), int(value[2:4]), int(value[4:6])
    return 1 <= month <= 12 and 1 <= day <= calendar.monthrange(2000 + year, month)[1]


def _luhn_valid(value: str) -> bool:
    digits = [int(digit) for digit in value]
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _mask(
    text: str,
    own_emp_id: str | None,
    own_name: str | None,
    include_identity: bool,
) -> MaskResult:
    found: dict[str, int] = {}

    def replace_matches(pattern: re.Pattern, kind: str, valid=None) -> None:
        nonlocal text

        def replacement(match: re.Match) -> str:
            if valid is not None and not valid(match):
                return match.group(0)
            found[kind] = found.get(kind, 0) + 1
            return f"[{kind}]"

        text = pattern.sub(replacement, text)

    replace_matches(_RRN, "주민번호", lambda match: _valid_rrn_date(match.group(1)))
    replace_matches(_CARD, "카드", lambda match: _luhn_valid(_digits(match.group(0))))
    replace_matches(_PHONE, "전화")
    replace_matches(_EMAIL, "이메일")

    def account_context(match: re.Match) -> bool:
        start, end = match.span()
        context = text[max(0, start - 16):min(len(text), end + 16)]
        return bool(_ACCOUNT_CONTEXT.search(context))

    def dashed_account(match: re.Match) -> bool:
        if not 10 <= len(_digits(match.group(0))) <= 14:
            return False
        if account_context(match):
            return True
        return not _PHONE_SHAPED_ACCOUNT.fullmatch(match.group(0))

    replace_matches(_DASHED_ACCOUNT, "계좌", dashed_account)
    replace_matches(_PLAIN_ACCOUNT, "계좌", account_context)

    if include_identity:
        own_id = re.sub(r"\s+", "", own_emp_id or "").upper()

        def replace_emp_id(match: re.Match) -> str:
            kind = "본인사번" if own_id and match.group(0).upper() == own_id else "사번"
            found[kind] = found.get(kind, 0) + 1
            return f"[{kind}]"

        text = _EMP_ID.sub(replace_emp_id, text)
        if own_name:
            pattern = re.compile(re.escape(own_name))

            def replace_name(match: re.Match) -> str:
                found["본인이름"] = found.get("본인이름", 0) + 1
                return "[본인이름]"

            text = pattern.sub(replace_name, text)

    return MaskResult(text, found)


def mask_text(text: str, own_emp_id: str | None = None, own_name: str | None = None) -> MaskResult:
    """자유 텍스트의 개인정보를 종류별 개수와 함께 마스킹한다."""
    return _mask(text, own_emp_id, own_name, include_identity=True)


def mask_answer(text: str, own_emp_id: str | None = None, own_name: str | None = None) -> MaskResult:
    """응답은 연락처 등만 가리고, 로그용일 때 요청자 식별자도 가린다."""
    return _mask(text, own_emp_id, own_name, include_identity=own_emp_id is not None or own_name is not None)


def pseudonymize(emp_id: str) -> str:
    """사번을 비가역적인 로그용 식별자로 바꾼다."""
    from . import config

    normalized_id = emp_id.strip().upper()
    digest = hmac.new(
        config.PSEUDO_KEY.encode("utf-8"),
        normalized_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"P-{digest[:12]}"


INPUT_NOTICE = "개인정보(주민번호, 계좌·카드번호, 연락처)는 입력하지 않아도 돼요. 입력하신 정보는 가려서 처리했어요."
