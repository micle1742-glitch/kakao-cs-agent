"""케이스 상태 블록: 도구 기록에서 이미 한 접수·이관·조회를 뽑는다."""

from app import actions, case_state, store


def test_render_lists_done_work(monkeypatch):
    monkeypatch.setattr(case_state, "TRACK_PHRASES", ["109"])
    monkeypatch.setattr(actions, "_operations", {
        "API_04": {"method": "GET"}, "API_10": {"method": "POST"}, "API_35": {"method": "POST"}})
    tc = "STATE-TEST-1"
    store.record_tool_call("u", tc, "API_04", {"order_id": "900000001"}, {"ok": True, "status": 200, "data": {}})
    store.record_tool_call("u", tc, "API_10", {"order_id": "900000001", "address": "예시로 1"},
                           {"ok": True, "status": 201, "data": {"receipt_no": "RCV-20370630-001"}})
    store.record_tool_call("u", tc, "API_35", {"lane": "HANDOFF", "target": "GENERAL"},
                           {"ok": True, "status": 201, "data": {"escalation_id": "HO-20370630-001"}})
    history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "상담 전화 109로 연락해 주세요"}]
    text = case_state.render(tc, history)
    assert "2번째 답" in text
    assert "HO-20370630-001" in text and "HANDOFF/GENERAL" in text
    assert "RCV-20370630-001" in text
    assert "API_04" in text
    assert "「109」 안내를 이미 1번" in text


def test_render_empty_case():
    text = case_state.render("STATE-TEST-EMPTY", [])
    assert "1번째 답" in text and "아직 한 접수" in text
