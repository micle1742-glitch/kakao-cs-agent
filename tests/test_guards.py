"""위기 사전 분류·사후 검사기·필수 조회·조회 캐시 비우기 시험 (LLM 없음)."""

import asyncio

import httpx

from app import actions, crisis, postcheck, store


def test_crisis_classify():
    assert crisis.classify("요즘 너무 힘들어서 죽고 싶어요") == crisis.CRISIS
    assert crisis.classify("마지막으로 꼭 한 번 먹어 보고 싶었어요") == crisis.CRISIS     # 이별 인사형 (애매 → 위기)
    assert crisis.classify("이제 주문할 일은 없을 것 같네요") == crisis.CRISIS
    assert crisis.classify("먹고 배탈이 났어요") == crisis.HEALTH
    assert crisis.classify("배탈도 났고 이제 다 끝내고 싶어요") == crisis.CRISIS           # 위기 표현이 이긴다
    assert crisis.classify("마지막으로 하나만 물어볼게요, 배송은 언제예요?") == crisis.NONE  # 이별 인사가 아님


def test_crisis_resource_once_per_case():
    first = crisis.reply([])
    assert "109" in first
    again = crisis.reply([{"role": "assistant", "content": first}])
    assert "109" not in again and "책임지고 처리" in again


def test_review_removes_forbidden_and_unsupported_amount():
    text, issues = postcheck.review("정책상 어렵습니다. 환불액은 12,500원입니다. 개수를 알려 주세요.",
                                    customer_text="얼마 돌려받나요", evidence='{"paid": 30000}')
    assert text == "개수를 알려 주세요." and len(issues) == 2
    kept, _ = postcheck.review("결제하신 30,000원을 확인했습니다.", customer_text="", evidence='{"paid": 30000}')
    assert "30,000원" in kept                                   # 근거에 있는 금액은 그대로


def test_review_adds_stop_payment_for_phishing():
    text, _ = postcheck.review("저희 문자가 아닙니다.", customer_text="문자 링크로 결제하려는 중이에요", evidence="")
    assert text.startswith("진행 중인 결제나 송금은 바로 멈춰 주세요.")
    text, _ = postcheck.review("결제는 멈춰 주세요. 저희 문자가 아닙니다.", customer_text="문자 링크로 결제하려는 중", evidence="")
    assert text.count("멈춰") == 1                               # 이미 있으면 넣지 않음


def test_fit_keeps_receipt_sentence():
    long = " ".join(["설명 문장입니다."] * 80) + " 접수번호는 HO-20370630-001이고, 7월 3일(금)까지 회신드립니다."
    out = postcheck.fit(long)
    assert len(out) <= postcheck.MAX_CHARS and "HO-20370630-001" in out


def test_needs_lookup():
    tc = "GUARD-1"
    assert postcheck.needs_lookup("주문 내역이 없습니다.", tc, {"API_02"})
    assert not postcheck.needs_lookup("보관은 냉장으로 해 주세요.", tc, {"API_02"})
    store.record_tool_call("u", tc, "API_02", {}, {"ok": True, "data": []})
    assert not postcheck.needs_lookup("주문 내역이 없습니다.", tc, {"API_02"})


def test_cache_cleared_after_submission(monkeypatch):
    calls = []

    async def fake_send(method, path, tc_id, params=None, body=None):
        calls.append((method, path))
        return httpx.Response(201 if method == "POST" else 200, json={"ok": 1}, request=httpx.Request(method, "http://x"))
    monkeypatch.setattr(actions, "_send", fake_send)
    actions._get_cache.clear()

    async def run():
        await actions.request("GET", "/api/shipments/1", "CACHE-1")
        await actions.request("GET", "/api/shipments/1", "CACHE-1")      # 캐시
        await actions.request("POST", "/api/orders/1/address", "CACHE-1", body={"address": "예시로 7"})
        await actions.request("GET", "/api/shipments/1", "CACHE-1")      # 접수 뒤엔 다시 서버로
    asyncio.run(run())
    assert [m for m, _ in calls] == ["GET", "POST", "GET"]
