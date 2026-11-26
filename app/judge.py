"""판정 도구(judge_rule): 같은 쟁점의 카드들 중 이번 케이스에 적용할 값을 코드로 고른다.

LLM이 판(시행일)과 권위를 직접 따지면 추론을 꺼 둔 상태에서 틀리기 쉽다. 그래서
  1. 기준 시점 정하기 — 카드의 basis, 없으면 rules.yaml의 쟁점별 기본값 (신고일·주문일·결제완료·오늘)
  2. 판 고르기 — 규정(현행·구판) 카드 중 기준 날짜에 시행 중인 판(시행일이 기준 날짜 이하인 것 중 가장 늦은 것).
     구판으로 분류된 문서라도 기준 날짜에 시행 중이면 그 판이 적용된다 (예: 개정 전에 한 주문)
  3. 권위 비교 — 남은 카드끼리 rules.yaml 권위 순서로. 이긴 값 + 진 쪽 '인정하되 근거로 안 씀' 문구
를 코드가 하고, LLM은 결과를 고객 말로만 바꾼다.

모르면 지어내지 않는다: 카드가 없거나, 시행일을 몰라 판을 못 고르거나, 같은 권위끼리 값이 다르면
'확인되지 않음 → 이관'을 돌려준다. 기준 날짜가 필요한데 없으면 그 날짜를 물으라고 돌려준다.
"""

import json
import logging

from app import calendar, prompt, rules
from kb.cards import norm, topic_key

logger = logging.getLogger("judge")
VERSIONED = ("rule", "old")  # 판(시행일)으로 고르는 분류 — 같은 규정의 현행판·옛 판
_cache = {"stamp": None, "cards": []}


def _cards():
    """kb 공개본의 카드 (version.json이 바뀌면 다시 읽음)."""
    path = prompt.KB_DIR / "cards.json"
    try:
        stat = path.stat()
        stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return []
    if stamp != _cache["stamp"]:
        _cache["cards"] = json.loads(path.read_text(encoding="utf-8"))
        _cache["stamp"] = stamp
    return _cache["cards"]


def _value(card):
    vals = card.get("values") or []
    return ", ".join(v["value"] for v in vals) if vals else card["rule"]


def _date(value):
    try:
        return calendar.parse_date(value) if value else None
    except ValueError:
        return None


def _unknown(topic, reason, **extra):
    return {"ok": False, "topic": topic, "result": "확인되지 않음", "action": "이관", "reason": reason, **extra,
            "note": "값을 지어내지 말고, 확인되지 않았다고 알린 뒤 담당자 이관을 접수하고 접수번호·회신 날짜를 안내한다."}


def find_topic(cards, topic):
    """주제 이름으로 카드 고르기: 같은 이름 → 한쪽이 다른 쪽을 포함하는 이름이 하나뿐일 때."""
    key = topic_key(topic)
    exact = [c for c in cards if topic_key(c["topic"]) == key]
    if exact:
        return exact, None
    names = sorted({c["topic"] for c in cards if key and (key in topic_key(c["topic"]) or topic_key(c["topic"]) in key)})
    if len(names) == 1:
        return [c for c in cards if c["topic"] == names[0]], None
    return [], names


def judge(args, cards=None):
    """도구 진입점. 판정 중 오류가 나도 지어내지 않게 '확인되지 않음 → 이관'으로 돌려준다."""
    try:
        return _judge(args, cards)
    except Exception as e:  # 카드 형식 이상 등
        logger.exception("판정 오류")
        return _unknown(str(args.get("topic") or ""), f"판정 중 오류: {type(e).__name__}")


def _judge(args, cards=None):
    cfg = rules.get()
    topic = str(args.get("topic") or "").strip()
    cards = _cards() if cards is None else cards
    group, similar = find_topic(cards, topic)
    if not group:
        return _unknown(topic, "이 쟁점의 카드가 없음" + (f" (비슷한 주제: {', '.join(similar)})" if similar else ""))

    by_rank = sorted(group, key=lambda c: c.get("authority") or 99)
    basis = next((c["basis"] for c in by_rank if c.get("basis")), None) \
        or cfg["topics"].get(group[0]["topic"]) or cfg["default_basis"]
    case_date = _date(args.get(basis)) or (_date(args.get("오늘")) if basis == "오늘" else None)

    versioned = [c for c in group if c.get("authorityId") in VERSIONED and c.get("status") != "인정만"]
    dated = [c for c in versioned if _date(c.get("effectiveDate"))]
    losers = []  # (카드, 진 이유 이름)
    pool = [c for c in group if c not in versioned or c.get("status") == "인정만"]
    if versioned:
        if dated and case_date is None:
            return {"ok": False, "topic": group[0]["topic"], "result": "기준 날짜 필요", "basis": basis,
                    "note": f"이 쟁점은 '{basis}' 기준으로 판이 갈린다. 고객에게 {basis}을(를) 확인하거나 조회한 뒤 다시 판정한다."}
        in_force = [c for c in dated if _date(c["effectiveDate"]) <= case_date] if case_date else []
        current = max((_date(c["effectiveDate"]) for c in in_force), default=None)
        for c in versioned:
            d = _date(c.get("effectiveDate"))
            if d is None:
                if dated:
                    losers.append((c, "시행일 미확인"))  # 판을 가릴 수 있는 카드가 따로 있다
                else:
                    pool.append(dict(c))
            elif current and d == current:
                rank, _ = rules.rank("rule")  # 기준 날짜에 시행 중인 판 = 그때의 현행 규정과 같은 권위
                pool.append({**c, "authority": rank, "authorityLabel": f"{basis} 당시 규정({c['effectiveDate']} 시행판)"})
            elif case_date and d > case_date:
                losers.append((c, f"{c['effectiveDate']} 시행판(기준 날짜 뒤에 시행)"))
            else:
                losers.append((c, f"이전 판({c['effectiveDate']} 시행)"))
        if dated and current is None:
            return _unknown(group[0]["topic"], f"{basis} {case_date} 당시 시행 중인 판을 찾지 못함", basis=basis)

    ranked = sorted([c for c in pool if c.get("status") != "인정만"],
                    key=lambda c: (c.get("authority") or 99, -(_date(c.get("effectiveDate")) or calendar.parse_date("1900-01-01")).toordinal()))
    if not ranked:
        return _unknown(group[0]["topic"], "근거로 쓸 카드가 없음 (모두 '인정만')")
    winner = ranked[0]
    rivals = [c for c in ranked[1:] if c.get("authority") == winner.get("authority") and norm(_value(c)) != norm(_value(winner))]
    if rivals:
        return _unknown(winner["topic"], "같은 권위의 카드끼리 값이 다르고 시행일로 가릴 수 없음",
                        values=[_value(winner)] + [_value(c) for c in rivals])
    losers += [(c, c.get("authorityLabel")) for c in pool if c is not winner and c not in ranked[:1]]

    ack, seen = [], set()
    for card, label in losers:
        value = _value(card)
        if norm(value) == norm(_value(winner)) or norm(value) in seen:
            continue
        seen.add(norm(value))
        ack.append(cfg["acknowledge"].format(loser_label=label, loser_value=value,
                                             winner_label=winner.get("authorityLabel"), winner_value=_value(winner)))
    claimed = str(args.get("claimed") or "").strip()
    result = {
        "ok": True, "topic": winner["topic"], "basis": basis, "caseDate": case_date.isoformat() if case_date else None,
        "value": _value(winner), "answer": winner["rule"], "authority": winner.get("authorityLabel"),
        "source": (winner.get("source") or {}).get("fileName") or (winner.get("source") or {}).get("bot"),
        "effectiveDate": winner.get("effectiveDate"), "acknowledge": ack,
        "note": "이 결과(value·answer)를 고객 말로 바꿔 안내한다. 고객이 진 쪽 내용을 말했으면 acknowledge 문구로 인정하되 근거로 쓰지 않는다.",
    }
    if claimed:
        result["claimedIsWinner"] = norm(claimed) in norm(_value(winner))
    logger.info("판정 %s: %s (%s, 기준 %s %s)", topic, result["value"], result["authority"], basis, result["caseDate"])
    return result


def tool_definition():
    """LOCAL_TOOLS 형식. 기준 시점 이름은 rules.yaml의 bases를 따른다."""
    bases = rules.get()["bases"]
    props = {
        "topic": {"type": "string", "description": "쟁점(카드 주제) 이름. kb 카드의 ## 제목을 그대로 쓴다"},
        "claimed": {"type": "string", "description": "고객이 주장한 값(있으면). 예: 안내문에 3일이라고 돼 있다 → 3일"},
    }
    for b in bases:
        props[b] = {"type": "string", "description": f"{b} (YYYY-MM-DD, 시각이 있으면 YYYY-MM-DD HH:MM). 조회·고객 말로 확인한 값만"}
    return {
        "name": "judge_rule",
        "description": "[판정] 기한·기간·금액처럼 판(시행일)과 권위에 따라 값이 갈리는 쟁점에서 이번 케이스에 적용할 값을 코드로 고른다. "
                       f"기준 시점({'·'.join(bases)})의 날짜로 시행 중인 판을 고르고 권위 순서로 비교해, 이긴 값과 "
                       "진 쪽을 인정하는 문구를 돌려준다. '확인되지 않음'이면 지어내지 말고 이관한다. 서버 호출이 아닌 계산이다.",
        "input_schema": {"type": "object", "properties": props, "required": ["topic"]},
    }
