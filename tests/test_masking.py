import hashlib
import hmac
import os

os.environ["PSEUDO_KEY"] = "test-pseudo-key"

from app import config  # noqa: E402
from app.masking import mask_answer, mask_text, pseudonymize


def test_masks_rrn_card_phone_email_and_accounts():
    text = (
        "주민번호 900501-1234567 / 900501 1234567 / 9005011234567, "
        "카드 4532-0151 1283-0366, 전화 010-1234-5678 02.1234.5678, "
        "메일 user.name@example.com, 계좌 123-456-7890, 통장 123456789012"
    )

    result = mask_text(text)

    assert result.text.count("[주민번호]") == 3
    assert "[카드]" in result.text
    assert result.text.count("[전화]") == 2
    assert "[이메일]" in result.text
    assert result.text.count("[계좌]") == 2
    assert result.found == {"주민번호": 3, "카드": 1, "전화": 2, "이메일": 1, "계좌": 2}


def test_invalid_rrn_date_and_luhn_card_are_not_masked():
    text = "991332-1234567 4532015112830367"

    result = mask_text(text)

    assert result.text == text
    assert result.found == {}


def test_non_pii_dates_amounts_and_internal_ids_are_unchanged():
    text = (
        "2026-11-12 2026.11.12 1,500,000원 제23조 별표 2 "
        "R000001 T00001 A0001 F0001 H00001 TR00001 D15 r03-0024"
    )

    result = mask_text(text)

    assert result.text == text
    assert result.found == {}


def test_own_and_other_employee_ids_and_own_name():
    result = mask_text(
        "E1584 E1412 이서준 박지현",
        own_emp_id="e1584",
        own_name="이서준",
    )

    assert result.text == "[본인사번] [사번] [본인이름] 박지현"
    assert result.found == {"본인사번": 1, "사번": 1, "본인이름": 1}


def test_account_without_hyphens_needs_nearby_account_keyword():
    plain_number = "123456789012"

    assert mask_text(plain_number).text == plain_number
    assert mask_text(f"계좌번호 {plain_number}").text == "계좌번호 [계좌]"


def test_answer_masking_preserves_identity_for_user_and_masks_it_for_logs():
    text = "이서준 E1584 010-1234-5678"

    assert mask_answer(text).text == "이서준 E1584 [전화]"
    assert mask_answer(text, own_emp_id="E1584", own_name="이서준").text == "[본인이름] [본인사번] [전화]"


def test_pseudonym_is_stable_and_does_not_expose_employee_id():
    first = pseudonymize("E1584")

    assert first == pseudonymize("E1584")
    assert first != pseudonymize("E1412")
    assert "E1584" not in first
    assert len(first) == 14 and first.startswith("P-")


def test_pseudonym_normalizes_employee_id_and_uses_hmac_sha256():
    expected_digest = hmac.new(
        config.PSEUDO_KEY.encode("utf-8"),
        b"E1584",
        hashlib.sha256,
    ).hexdigest()

    assert pseudonymize("e1584") == pseudonymize(" E1584 ")
    assert pseudonymize(" E1584 ") == f"P-{expected_digest[:12]}"
