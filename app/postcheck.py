"""사후 검사기: LLM 답을 보내기 전에 코드가 마지막으로 본다. 고칠 때는 LLM을 다시 부르지 않고 정해진 방식으로 고친다.

1. 금지 문구(rules.yaml forbidden)가 든 문장을 뺀다
2. 고객이 결제·송금을 하려는 피싱 상황인데 '멈추라'는 말이 없으면 맨 앞에 넣는다
3. 답의 금액이 근거(도구 결과·고객 말·케이스 정보·지식)에 없으면 그 문장을 뺀다 — 틀린 금액보다 빠진 금액이 낫다.
   날짜는 표기가 다양해 빼지 않고 기록만 남긴다
4. fit(): 말풍선 500자를 넘으면 뒤 문장부터 빼되, 접수번호·날짜가 든 문장은 남긴다 (예전엔 끝을 잘라 접수번호가 잘렸다)
5. needs_lookup(): '없음·불가' 결론인데 이 케이스에 조회가 없으면 True (handler가 한 번 다시 시키고, 그래도면 이관)

handler의 순서: review(1~3) → escalation.ensure('전달하겠습니다'인데 기록 없음, 지어낸 접수번호) → fit(4)
"""

import json
import logging
import re

from app import rules, store

logger = logging.getLogger("postcheck")
MAX_CHARS = 500
# 문장을 다 빼서 남은 게 없을 때 (escalation.ensure가 이어서 이관 여부를 본다)
EMPTY_FALLBACK = "말씀하신 내용을 확인하고 있습니다. 확인이 필요한 부분은 담당자에게 넘겨 정확히 안내드리겠습니다."
SENTENCE = re.compile(r"(?<=[.!?。])\s+|\n+")
AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})+원|\d+원")
DATE = re.compile(r"\d{1,2}월 ?\d{1,2}일|(?<!\d)\d{1,2}[-/]\d{1,2}(?!\d)")
KEEP = re.compile(r"\d{1,2}월 ?\d{1,2}일|[A-Z]{2,4}-\d{8}-\d{3}")


def sentences(text):
    return [s.strip() for s in SENTENCE.split(text or "") if s.strip()]


def _digits(s):
    return re.sub(r"\D", "", s)


def evidence_text(tc_id, history, customer_text, case_info, knowledge=""):
    """답의 숫자를 대조할 근거: 이 케이스의 도구 결과·인자, 고객이 한 말, 케이스 정보, 지식(프롬프트)."""
    parts = [json.dumps(c, ensure_ascii=False) for c in store.load_tool_calls(tc_id)]
    parts += [m["content"] for m in history if m["role"] == "user"] + [customer_text or ""]
    parts += [json.dumps(case_info or {}, ensure_ascii=False), knowledge or ""]
    return "\n".join(parts)


def review(reply, *, customer_text, evidence):
    cfg = rules.get()
    issues = []
    parts = sentences(reply)

    # 1. 금지 문구
    kept = [s for s in parts if not any(p in s for p in cfg["forbidden"])]
    if len(kept) != len(parts):
        issues.append(f"금지 문구 문장 {len(parts) - len(kept)}개 뺌")
    parts = kept

    # 3. 근거 없는 금액
    ev_digits = {_digits(a) for a in AMOUNT.findall(evidence)} | set(re.findall(r"\d{3,}", evidence.replace(",", "")))
    kept = []
    for s in parts:
        bad = [a for a in AMOUNT.findall(s) if _digits(a) not in ev_digits]
        if bad and len(parts) > 1:
            issues.append(f"근거 없는 금액 {bad} 문장 뺌")
            continue
        kept.append(s)
    parts = kept
    for d in DATE.findall(" ".join(parts)):
        if d not in evidence and _digits(d) not in _digits(evidence):
            issues.append(f"근거를 찾지 못한 날짜 {d} (그대로 둠)")

    # 2. 피싱인데 멈추라는 말이 없음
    ph = cfg["phishing"]
    if any(re.search(p, customer_text or "") for p in ph.get("detect", ())):
        if not any(k in " ".join(parts) for k in ph.get("stop_ok", ())) and ph.get("stop_sentence"):
            parts.insert(0, ph["stop_sentence"])
            issues.append("피싱 상황에 결제 중단 안내 넣음")

    text = " ".join(parts)
    if not text.strip():
        text = EMPTY_FALLBACK
        issues.append("남은 문장이 없어 기본 문구")
    if issues:
        logger.warning("사후 검사: %s", "; ".join(issues))
    return text, issues


def fit(text, limit=MAX_CHARS):
    """4. 길이 맞추기 (맨 마지막에): 뒤 문장부터 빼되 접수번호·날짜가 든 문장은 남긴다."""
    if len(text) <= limit:
        return text
    keep = sentences(text)
    for i in range(len(keep) - 1, 0, -1):
        if len(" ".join(keep)) <= limit:
            break
        if not KEEP.search(keep[i]):
            keep.pop(i)
    out = " ".join(keep)
    logger.warning("답이 %d자라 문장을 줄임 → %d자", len(text), len(out))
    return out if len(out) <= limit else out[:limit - 1] + "…"


# '없음·불가' 결론 — 조회 결과가 있어야만 허용한다
NEGATIVE = re.compile(r"(주문|내역|기록|결제|배송|상품|회원)[^.!?\n]{0,15}(없습니다|없어요|없으세요|확인되지 않|조회되지 않|찾을 수 없)"
                      r"|불가합니다|불가능합니다|해 ?드릴 수 없|할 수 없습니다")


def needs_lookup(reply, tc_id, get_tool_names):
    """답이 '없음·불가'로 결론 내는데 이 케이스에 조회(GET)가 한 번도 없으면 True.

    get_tool_names: 조회 도구 이름 집합 (actions의 GET 도구)."""
    if not NEGATIVE.search(reply or ""):
        return False
    return not any(c["name"] in get_tool_names for c in store.load_tool_calls(tc_id))
