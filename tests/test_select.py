"""카드 골라 넣기 시험 (지어낸 카드)."""

import json
from pathlib import Path

from app import select

CARDS = json.loads((Path(__file__).resolve().parent.parent / "traps" / "fixtures" / "kb" / "cards.json")
                   .read_text(encoding="utf-8"))


def topics(cards):
    return {c["topic"] for c in cards}


def test_picks_matching_topic_group():
    chosen = select.pick(CARDS, "반품 접수 기한이 며칠이에요? 박스엔 3일이라던데")
    assert "반품 접수 기한" in topics(chosen)
    assert {"K-ret-new", "K-ret-old"} <= {c["id"] for c in chosen}   # 판이 다른 카드를 묶음째
    assert len(chosen) <= select.MAX_CARDS


def test_nothing_matches_nothing_added():
    assert select.pick(CARDS, "안녕하세요") == []


def test_earlier_turns_count_but_less():
    chosen = select.pick(CARDS, "그럼 언제 나가요?", earlier=["배너에 오늘 출고라고 써 있던데요"])
    assert "출고 요일" in topics(chosen)


def test_cap_respected():
    many = [dict(c, id=f"{c['id']}-{i}") for i in range(5) for c in CARDS]
    assert len(select.pick(many, "반품 접수 기한 출고 요일 포장 기준 가격 인하 보상")) <= select.MAX_CARDS
