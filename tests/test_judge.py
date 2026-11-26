"""판정 도구·헌법·권위 설정 시험 (지어낸 카드, LLM 없음)."""

import asyncio
import json
from pathlib import Path

import yaml

from app import constitution, judge, rules

ROOT = Path(__file__).resolve().parent.parent
CARDS = json.loads((ROOT / "traps" / "fixtures" / "kb" / "cards.json").read_text(encoding="utf-8"))


def test_version_by_report_date():
    r = judge.judge({"topic": "반품 접수 기한", "신고일": "2037-06-30"}, CARDS)
    assert r["value"] == "5일" and r["basis"] == "신고일"
    assert any("'3일'" in a for a in r["acknowledge"])        # 구판은 인정 문구로


def test_same_cards_old_version_before_revision():
    r = judge.judge({"topic": "반품 접수 기한", "신고일": "2037-06-10"}, CARDS)
    assert r["value"] == "3일"                                   # 개정(06-20) 전 신고면 옛 판
    assert any("시행판(기준 날짜 뒤에 시행)" in a for a in r["acknowledge"])


def test_basis_differs_by_topic():
    dates = {"주문일": "2037-06-10", "신고일": "2037-06-30"}
    assert judge.judge({"topic": "반품 접수 기한", **dates}, CARDS)["value"] == "5일"
    assert judge.judge({"topic": "가격 인하 보상", **dates}, CARDS)["value"] == "기간 제한 없이 차액 환불"


def test_missing_basis_date_asks_instead_of_guessing():
    r = judge.judge({"topic": "반품 접수 기한"}, CARDS)
    assert r["ok"] is False and r["result"] == "기준 날짜 필요" and r["basis"] == "신고일"


def test_promo_loses_to_record_but_is_acknowledged():
    r = judge.judge({"topic": "출고 요일", "오늘": "2037-06-30", "claimed": "주문 당일"}, CARDS)
    assert r["value"] == "월·수·금" and r["claimedIsWinner"] is False
    assert "홍보물·발언·배너" in r["acknowledge"][0]


def test_unknown_escalates():
    assert judge.judge({"topic": "포장 기준"}, CARDS)["action"] == "이관"      # 같은 권위·다른 값·시행일 없음
    assert judge.judge({"topic": "없는 쟁점"}, CARDS)["result"] == "확인되지 않음"


def test_decided_loser_is_never_winner():
    cards = [dict(c) for c in CARDS]
    for c in cards:
        if c["id"] == "K-ship-rec":
            c["status"] = "인정만"                                # 사람이 판정에서 진 쪽으로 정함
    r = judge.judge({"topic": "출고 요일", "오늘": "2037-06-30"}, cards)
    assert r["value"] == "주문 당일"


def test_authority_order_comes_from_file(tmp_path, monkeypatch):
    cfg = yaml.safe_load((ROOT / "rules.yaml").read_text(encoding="utf-8"))
    order = cfg["authority"]
    promo = next(a for a in order if a["id"] == "promo")
    order.remove(promo)
    order.insert(0, promo)                                     # 대회 당일 순서를 바꾼 경우
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("RULES_FILE", str(path))
    assert rules.rank("promo")[0] == 1
    assert "1. 홍보물·발언·배너" in constitution.build()


def test_constitution_size_and_place():
    from app import prompt
    text = constitution.build()
    assert len(text) <= constitution.MAX_CHARS
    for s in ("권위 순서", "judge_rule", "개인정보", "정책상 어렵습니다"):
        assert s in text
    assert prompt.build_system_prompt().startswith("<constitution>")


def test_judge_tool_registered_with_bases():
    from app import actions
    schema = actions.LOCAL_TOOLS["judge_rule"][0]["input_schema"]["properties"]
    assert {"topic", "claimed", *rules.get()["bases"]} <= set(schema)
    out = asyncio.run(actions.execute("judge_rule", {"topic": "없는 쟁점"}, None))
    assert out["action"] == "이관"
