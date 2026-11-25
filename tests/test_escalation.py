"""이관 보장 규칙의 순수 함수 시험."""

from app import escalation as e


def test_promise_detected():
    for text in ["담당자에게 전달하겠습니다.", "담당 부서에 넘겨 두었습니다.", "확인 후 다시 안내드릴게요.",
                 "이관해 드리겠습니다.", "관련 부서로 확인 요청드리겠습니다."]:
        assert e.promises_handoff(text), text


def test_plain_answer_is_not_promise():
    for text in ["서늘한 곳에 보관해 주세요.", "배송지 변경을 접수했습니다.", "환불은 원결제수단으로 진행됩니다."]:
        assert not e.promises_handoff(text), text


def test_key_uses_target_field_or_reason_prefix():
    assert e.key_of({"lane": "HANDOFF", "target": "DELIVERY"}) == ("HANDOFF", "DELIVERY")
    assert e.key_of({"lane": "HANDOFF", "reason": "[PAYMENT·높음] 이중 결제"}) == ("HANDOFF", "PAYMENT")
    assert e.key_of({"reason": "사유만"}) == (e.DEFAULT_LANE, "")


def test_receipt_found_by_format():
    assert e.receipt_of({"ok": True, "data": {"escalation_id": "HO-20370630-004"}}) == "HO-20370630-004"
    assert e.receipt_of({"ok": False, "data": {"escalation_id": "HO-20370630-004"}}) is None
    assert e.receipt_of({"ok": True, "data": {}}) is None


def test_deadline_text():
    assert e.deadline_text({"escalation_deadline": "2037-07-03(금) 24:00"}) == "7월 3일(금)"
    assert e.deadline_text({}) is None


def test_receipt_digits_are_not_a_date():
    assert not e.DATE_IN_TEXT.search("접수번호는 HO-20370630-001입니다.")
    assert e.DATE_IN_TEXT.search("7월 3일(금)까지")
    assert e.DATE_IN_TEXT.search("07-03까지")
