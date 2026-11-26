"""충돌 찾기와 사람 판정.

같은 주제에서 같은 이름의 값이 카드마다 다르면 충돌이다 (예: "하자 접수 기한" 7일 vs 3일).
코드는 권위 순서(작을수록 강함) → 시행일(늦을수록 우선)로 '제안'만 하고, 결정은 사람이 decisions.yaml에 적는다.
사람이 정하기 전에도 봇은 두 카드를 모두 보되, 제안된 쪽이 우선이라는 표시를 함께 본다.

decisions.yaml 형식 (충돌 id는 관련 카드로 만든 고정 id라 다시 빌드해도 같다)
  C1a2b3c4d5:
    choose: 7일          # 이 값을 따른다 / suggested = 제안대로 / both = 둘 다 유효(조건이 다름)
    note: 구판 안내문은 인정만
"""

import hashlib
from pathlib import Path

import yaml

from kb.cards import norm, topic_key


def _conflict_id(topic, name, card_ids):
    raw = f"{topic_key(topic)}|{norm(name)}|{'|'.join(sorted(card_ids))}"
    return "C" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _suggest(options):
    """가장 강한 권위 → 같은 권위면 가장 늦은 시행일. 그래도 같으면 None(사람이 판정)."""
    ranked = sorted(options, key=lambda o: (o["rank"], _neg_date(o["date"])))
    if len(ranked) > 1 and (ranked[0]["rank"], ranked[0]["date"]) == (ranked[1]["rank"], ranked[1]["date"]):
        return None
    return ranked[0]["value"]


def _neg_date(date):
    # 늦은 날짜가 앞에 오게 (없는 날짜는 맨 뒤)
    return tuple(-int(x) for x in date.split("-")) if date else (1,)


def find(cards):
    """[{id, topic, name, options: [{value, cards, rank, date}], suggestion}]."""
    groups, labels = {}, {}
    for c in cards:
        for v in c.get("values", ()):
            key = (topic_key(c["topic"]), norm(v["name"]))
            groups.setdefault(key, []).append((c, v["value"]))
            labels.setdefault(key, (c["topic"], v["name"]))  # 보여 줄 이름은 처음 나온 표기
    conflicts = []
    for key, items in sorted(groups.items()):
        by_value = {}
        for c, value in items:
            by_value.setdefault(norm(value), {"value": value, "cards": [], "rank": 99, "date": None})
            opt = by_value[norm(value)]
            opt["cards"].append(c["id"])
            opt["rank"] = min(opt["rank"], c.get("authority") or 99)
            d = c.get("effectiveDate")
            if d and (opt["date"] is None or d > opt["date"]):
                opt["date"] = d
        if len(by_value) < 2:
            continue
        options = sorted(by_value.values(), key=lambda o: (o["rank"], _neg_date(o["date"])))
        topic, name = labels[key]
        ids = [cid for o in options for cid in o["cards"]]
        conflicts.append({"id": _conflict_id(topic, name, ids), "topic": topic, "name": name,
                          "options": options, "suggestion": _suggest(options)})
    return conflicts


def load_decisions(path):
    if not path or not Path(path).exists():
        return {}
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def apply(cards, conflicts, decisions):
    """판정을 카드에 반영: 진 쪽 카드는 status='인정만'(있었던 사실로 인정, 근거로 쓰지 않음).

    사람이 아직 안 정한 충돌은 카드에 conflict 표시만 남긴다 (봇이 우선순위 규칙대로 판단).
    """
    by_id = {c["id"]: c for c in cards}
    for conflict in conflicts:
        decision = decisions.get(conflict["id"]) or {}
        choose = str(decision.get("choose", "")).strip()
        if choose == "suggested":
            choose = conflict["suggestion"] or ""
        conflict["decided"] = choose or None
        for opt in conflict["options"]:
            for cid in opt["cards"]:
                card = by_id.get(cid)
                if not card:
                    continue
                card.setdefault("conflicts", []).append(conflict["id"])
                if choose and choose != "both" and norm(opt["value"]) != norm(choose):
                    card["status"] = "인정만"
                    card["statusNote"] = decision.get("note") or f"충돌 {conflict['id']} 판정: {choose}"
    return cards


def write_review(conflicts, cards, md_path, decisions_path):
    """사람이 읽는 검토 파일(conflicts.md)과 판정 틀(decisions.yaml — 이미 적은 판정은 보존)."""
    by_id = {c["id"]: c for c in cards}
    lines = ["# 충돌 검토", "",
             "같은 주제에서 값이 다른 카드입니다. `decisions.yaml`의 `choose`에 따를 값(또는 suggested / both)을 적고 다시 빌드하세요.",
             "정하기 전에도 봇은 '제안' 쪽을 우선하라는 표시와 함께 두 카드를 모두 봅니다.", ""]
    if not conflicts:
        lines.append("충돌 없음.")
    for c in conflicts:
        state = f"판정: **{c['decided']}**" if c.get("decided") else "판정 전"
        lines += [f"## {c['id']} · {c['topic']} — {c['name']}", f"- 제안: {c['suggestion'] or '사람 판정 필요(권위·시행일이 같음)'} · {state}"]
        for o in c["options"]:
            lines.append(f"- **{o['value']}**")
            for cid in o["cards"]:
                card = by_id.get(cid, {})
                src = card.get("source", {})
                where = " ".join(x for x in (src.get("fileName") or src.get("bot") or "?", _loc(src)) if x)
                date = f" · {card['effectiveDate']} 시행" if card.get("effectiveDate") else ""
                lines.append(f"  - [{card.get('authorityLabel')}] {where}{date} — “{card.get('evidence', '')[:120]}”")
        lines.append("")
    Path(md_path).write_text("\n".join(lines), encoding="utf-8")

    old = load_decisions(decisions_path)
    merged = {c["id"]: old.get(c["id"]) or {"choose": "", "note": f"{c['topic']} · {c['name']}: "
                                             + " / ".join(o["value"] for o in c["options"])} for c in conflicts}
    for k, v in old.items():  # 지금은 사라진 충돌의 판정도 지우지 않는다
        merged.setdefault(k, v)
    Path(decisions_path).write_text(
        "# 충돌 판정. choose: 따를 값 / suggested / both. 비워 두면 판정 전.\n"
        + yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _loc(src):
    loc = src.get("location") or {}
    return " ".join(f"{k} {v}" for k, v in loc.items())
