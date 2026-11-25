"""이관 보장: 이관은 접수 기록(접수번호)을 남겨야 이관이다. 말로만 "전달하겠습니다"는 이관이 아니다.

프롬프트로 부탁만 해서는 깨지는 절차라서 코드가 지킨다.
1. 답에 이관 약속("담당자에게 전달하겠습니다" 등)이 있는데 접수번호가 없으면
   → 이 케이스에 이미 한 이관이 있으면 그 번호를, 없으면 시스템이 이관을 접수해 번호와 회신 날짜를 답 끝에 붙인다.
2. 도구 왕복 상한에 걸리거나 답 생성 시간이 넘으면 → 이관을 먼저 접수하고 그 번호로 답한다.
   ("확인 후 다시 안내드릴게요" 같은 고정 문구로 끝내지 않는다)
3. 같은 (레인, 대상) 이관은 케이스당 한 번 → 사유 문장이 달라도 앞선 접수 결과를 돌려준다. 대상이 다르면 따로 접수한다.

이관 도구 이름·기본 레인·대상·우선순위·접수번호 형식은 환경변수로 바꾼다 (대회마다 API가 다르므로).
"""

import json
import logging
import os
import re

from app import actions, store

TOOL = os.getenv("ESCALATION_TOOL") or "API_35"
DEFAULT_LANE = os.getenv("ESCALATION_DEFAULT_LANE") or "HANDOFF"
# 판단이 서지 않을 때의 대상. 일반 운영 담당이 받아서 다시 나눈다
# (기본값은 예시. 대회의 실제 레인·대상·우선순위·접수번호 형식은 .env로 넣는다)
DEFAULT_TARGET = os.getenv("ESCALATION_DEFAULT_TARGET") or "GENERAL"
DEFAULT_PRIORITY = os.getenv("ESCALATION_DEFAULT_PRIORITY") or "보통"
RECEIPT = re.compile(os.getenv("ESCALATION_RECEIPT_PATTERN") or r"HO-\d{8}-\d{3}")

# 담당자에게 넘긴다(넘겼다)는 말. 이 말이 있으면 접수번호가 함께 있어야 한다
PROMISE = re.compile(
    r"(담당자|담당 ?부서|담당 ?팀|담당 ?직원|관련 ?부서)[^.!?\n]{0,15}(전달|연결|넘기|넘겨|이관|확인 ?요청|요청드리)"
    r"|전달(하겠|해 ?드리겠|드리겠|해 ?두겠|해 ?드릴게|드릴게)"
    r"|이관(하겠|해 ?드리겠|해 ?두었|했습니다|해 ?드렸)"
    r"|확인 ?후 ?(다시 )?(연락|안내|회신)(드리겠|드릴게|해 ?드리겠|해 ?드릴게)"
)
# 답에 날짜가 이미 있는지 ("7월 3일", "07-03", "7/3")
#  (숫자 앞뒤를 막아 접수번호 HO-20370630-001 속 숫자를 날짜로 읽지 않게)
DATE_IN_TEXT = re.compile(r"\d{1,2}월 ?\d{1,2}일|(?<!\d)\d{1,2}[-/]\d{1,2}(?!\d)")
REASON_TARGET = re.compile(r"^\s*\[([A-Z_]+)")
# 시간 초과·오류 때 보내는 답의 첫머리 (test_tc.py가 실패 응답을 셀 때 씀)
FALLBACK_PREFIX = "확인이 늦어"
WEEKDAY_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\((.)\)")

logger = logging.getLogger("escalation")


def promises_handoff(text):
    return bool(text and PROMISE.search(text))


def receipt_of(result):
    """도구 결과에서 접수번호를 찾는다 (필드 이름은 대회마다 달라서 형식으로 찾음)."""
    if not result or not result.get("ok"):
        return None
    m = RECEIPT.search(json.dumps(result, ensure_ascii=False))
    return m.group(0) if m else None


def key_of(args):
    """이관의 중복 판단 기준 (레인, 대상). 대상 필드가 없으면 사유 맨 앞 [대상·우선순위]에서 읽는다."""
    args = args or {}
    target = args.get("target")
    if not target:
        m = REASON_TARGET.match(str(args.get("reason", "")))
        target = m.group(1) if m else ""
    return (args.get("lane") or DEFAULT_LANE, target)


def done_in_case(tc_id):
    """이 케이스에서 접수된 이관 [(키, 접수번호, 결과)] (오래된 것부터, 같은 접수번호는 한 번)."""
    seen, out = set(), []
    for c in store.load_tool_calls(tc_id):
        if c["name"] != TOOL:
            continue
        receipt = receipt_of(c["result"])
        if receipt and receipt not in seen:
            seen.add(receipt)
            out.append((key_of(c["args"]), receipt, c["result"]))
    return out


def previous(name, args, tc_id):
    """같은 (레인, 대상) 이관이 이미 있으면 그 결과(다시 보내지 않음), 아니면 None."""
    if name != TOOL:
        return None
    key = key_of(args)
    for k, receipt, result in done_in_case(tc_id):
        if k == key:
            logger.warning("같은 대상·레인 이관 %s이 이미 있어 다시 보내지 않음 (%s)", key, receipt)
            return {**result, "duplicate": True,
                    "note": f"같은 대상·레인 이관이 이 상담에서 이미 접수되어({receipt}) 다시 보내지 않았습니다. "
                            "앞선 접수번호와 회신 날짜를 안내하세요."}
    return None


def latest_receipt(tc_id):
    done = done_in_case(tc_id)
    return done[-1][1] if done else None


def deadline_text(case_info):
    """케이스 정보의 이관 회신 기한 "2037-07-03(금) 24:00" → "7월 3일(금)"."""
    m = WEEKDAY_DATE.search((case_info or {}).get("escalation_deadline") or "")
    if not m:
        return None
    return f"{int(m.group(2))}월 {int(m.group(3))}일({m.group(4)})"


def receipt_sentence(receipt, case_info):
    deadline = deadline_text(case_info)
    when = f"{deadline}까지" if deadline else "영업일 3일 안에"
    return f"접수번호는 {receipt}이고, {when} 담당자가 회신드립니다."


def _forced_args(why, customer_text):
    tool = next((t for t in actions.TOOLS if t["name"] == TOOL), None)
    props = (tool or {}).get("input_schema", {}).get("properties", {})
    text = " ".join((customer_text or "").split())[:300]
    values = {
        "lane": DEFAULT_LANE, "target": DEFAULT_TARGET, "priority": DEFAULT_PRIORITY,
        "reason": f"[{DEFAULT_TARGET}·{DEFAULT_PRIORITY}] 시스템 자동 이관 — {why}. "
                  f"확인된 사실: 고객 문의 접수. 미확인: 처리 가능 여부. 고객 문의: {text}",
        "summary": text[:200],
    }
    # 이관 도구가 받는 필드만 보낸다 (명세를 못 읽었으면 레인·사유만)
    keys = [k for k in values if k in props] if props else ["lane", "reason"]
    return {k: values[k] for k in keys}


async def force(user_key, tc_id, why, customer_text):
    """시스템이 이관을 접수하고 접수번호를 반환. 이관 도구가 없거나 실패하면 None."""
    if not tc_id or TOOL not in actions._operations:
        return None
    args = _forced_args(why, customer_text)
    result = previous(TOOL, args, tc_id) or await actions.execute(TOOL, args, tc_id)
    store.record_tool_call(user_key, tc_id, TOOL, args, result)
    receipt = receipt_of(result)
    if receipt:
        logger.warning("시스템 자동 이관 %s (%s)", receipt, why)
    else:
        logger.error("시스템 자동 이관 실패 (%s): %s", why, result)
    return receipt


async def ensure(reply, *, user_key, tc_id, case_info, customer_text, forced_receipt=None, this_turn=None):
    """답의 이관 안내가 실제 기록과 맞게 고친다.

    1. 답에 기록에 없는 접수번호가 있으면(모델이 지어냄 — 문서 예시 번호를 베끼는 일이 실제로 있었다)
       실제 이관 번호로 바꾼다. 이번 턴에 한 이관이 없으면 새로 접수한다.
    2. 이관을 말하는데 접수번호가 없으면 접수번호(필요하면 새 이관)와 회신 날짜를 붙인다.
    3. 접수번호는 있는데 회신 날짜가 없으면 날짜만 붙인다 (절차서: 회신 날짜를 반드시 안내).
    this_turn: 이번 턴에 모델이 접수한 이관 번호.
    """
    if not reply:
        return reply
    found = list(dict.fromkeys(RECEIPT.findall(reply)))
    if found:
        known = {r for _, r, _ in done_in_case(tc_id)}
        fake = [r for r in found if r not in known]
        if fake:
            real = (forced_receipt or this_turn
                    or await force(user_key, tc_id, "답변에 기록에 없는 접수번호가 있음(모델이 지어냄)", customer_text))
            if real:
                logger.warning("기록에 없는 접수번호 %s를 실제 번호 %s로 바꿈", fake, real)
                for r in fake:
                    reply = reply.replace(r, real)
            else:
                logger.error("기록에 없는 접수번호 %s가 답에 있으나 이관을 접수하지 못함", fake)
        deadline = deadline_text(case_info)
        if deadline and not DATE_IN_TEXT.search(reply):
            return f"{reply.rstrip()} {deadline}까지 담당자가 회신드립니다."
        return reply
    if not (forced_receipt or promises_handoff(reply)):
        return reply
    receipt = (forced_receipt or this_turn or latest_receipt(tc_id)
               or await force(user_key, tc_id, "답변에서 담당자 이관을 안내했으나 접수 기록이 없음", customer_text))
    if not receipt:
        return reply
    return f"{reply.rstrip()} {receipt_sentence(receipt, case_info)}"


async def fallback_reply(*, user_key, tc_id, case_info, customer_text, why, this_turn=None):
    """답을 만들지 못했을 때(시간 초과·오류)의 답. 이관을 남기고 번호·회신 날짜로 답한다.

    this_turn: 이번 턴에 이미 접수된 이관 번호. 없으면 새로 접수한다(같은 레인·대상이 앞서 있으면 그 번호).
    """
    receipt = this_turn or await force(user_key, tc_id, why, customer_text)
    if receipt:
        return (f"확인이 늦어져 담당자에게 바로 넘겨 두었습니다. {receipt_sentence(receipt, case_info)} "
                "처음부터 다시 말씀하지 않으셔도 됩니다.")
    # 이관도 못 남긴 경우(도구 없음·서버 오류): 접수했다고 말하지 않는다
    return "확인이 늦어지고 있어 죄송합니다. 같은 내용을 한 번만 더 보내 주시면 바로 이어서 처리하겠습니다."
