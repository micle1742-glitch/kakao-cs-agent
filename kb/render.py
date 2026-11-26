"""카드 → 시스템 프롬프트에 넣을 글.

주제별로 묶고, 권위가 강한 카드부터 쓴다. 카드마다 권위·출처·시행일·기준 시점을 붙여 모델이 충돌을
우선순위대로 풀 수 있게 하고, 판정에서 진 카드는 '근거로 쓰지 않음'으로 따로 표시한다.
"""

from app import rules
from kb.conflicts import _neg_date

HEADER = """<kb>
아래는 수집한 문서·대화에서 뽑은 규칙 카드다. [ ] 안은 권위·출처·시행일·적용 기준 시점이다.
같은 주제에서 카드끼리 다르면 헌법의 권위 순서({order})대로 강한 쪽을, 같은 권위면 기준 시점에 시행 중인 판을 따른다.
값이 걸린 쟁점(기한·금액·기간)은 judge_rule 도구로 판정 결과를 받아 그대로 안내한다.
'근거로 쓰지 않음' 카드는 고객이 그 내용을 말하면 있었던 사실로 인정하되 처리 근거로 쓰지 않는다."""


def _tag(card):
    src = card.get("source", {})
    where = src.get("fileName") or (f"{src.get('bot')}의 말" if src.get("bot") else "")
    loc = " ".join(f"{k} {v}" for k, v in (src.get("location") or {}).items())
    parts = [card.get("authorityLabel") or "", f"{where} {loc}".strip()]
    if card.get("code"):
        parts.append(card["code"])
    if card.get("effectiveDate"):
        parts.append(f"{card['effectiveDate']} 시행")
    if card.get("basis"):
        parts.append(f"기준 {card['basis']}")
    return " · ".join(p for p in parts if p)


def render(cards, conflicts=()):
    pending = {c["id"]: c for c in conflicts if not c.get("decided")}
    by_topic = {}
    for card in cards:
        by_topic.setdefault(card["topic"], []).append(card)
    lines = [HEADER.format(order=" > ".join(a["label"] for a in rules.get()["order"]))]
    for topic in sorted(by_topic):
        # 근거로 쓰는 카드 먼저 → 강한 권위 먼저 → 늦은 시행일 먼저
        group = sorted(by_topic[topic], key=lambda c: (c.get("status") == "인정만", c.get("authority") or 99,
                                                       _neg_date(c.get("effectiveDate"))))
        lines.append(f"\n## {topic}")
        notes = {cid for c in group for cid in c.get("conflicts", ()) if cid in pending}
        for cid in sorted(notes):
            suggestion = pending[cid]["suggestion"]
            lines.append(f"(값이 다른 카드가 있음 — {pending[cid]['name']}: "
                         + (f"우선 '{suggestion}'" if suggestion else "권위·시행일이 같아 담당 확인 필요") + ")")
        for card in group:
            prefix = "(근거로 쓰지 않음) " if card.get("status") == "인정만" else ""
            lines.append(f"- {prefix}{card['rule']} [{_tag(card)}]")
    lines.append("</kb>")
    return "\n".join(lines)
