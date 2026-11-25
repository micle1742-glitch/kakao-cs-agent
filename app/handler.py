"""MESSAGE_CREATE 메시지 하나 처리: LLM 답변 생성 → 콜백 답장."""

import asyncio
import logging
import os
import re
import time

from app import actions, case, case_state, escalation, kakao, llm, media, store
from app.prompt import build_system_prompt

logger = logging.getLogger("handler")

# botUserKey별 대화 기록·케이스 정보는 store(sqlite)에 저장해 재시작 뒤에도 이어진다.
# 새 case metadata가 오면 이전 기록을 내리고 새 케이스로 시작하므로, 불러오는 기록은 이번 케이스 것뿐이다.
# 케이스당 응답이 10건(메시지 20개)이라 케이스 전체를 넘긴다. 이 값은 이상 상황에서 입력이 끝없이 커지지 않게 막는 상한
HISTORY_SIZE = int(os.getenv("HISTORY_SIZE") or "40")

SYSTEM_PROMPT = build_system_prompt()
ENABLE_TOOLS = os.getenv("ENABLE_TOOLS", "false").lower() == "true"
TOOLS = actions.load_tools() if ENABLE_TOOLS else None
# 수신부터 답장까지 쓸 수 있는 최대 시간 (평가는 60초 무응답이면 케이스 종료)
REPLY_DEADLINE = float(os.getenv("REPLY_DEADLINE", "45"))
# 그중 시간 초과 시 이관을 접수할 몫으로 남겨 두는 시간. LLM은 (남은 시간 - 이 값)까지만 기다린다
ESCALATION_RESERVE = float(os.getenv("ESCALATION_RESERVE") or "8")


def user_key_of(d):
    """대화 기록·케이스 정보를 저장하는 사용자 키 = botUserKey. 없으면 None."""
    key = d.get("botUserKey")
    return str(key) if key else None


def _preview(text, limit=200):
    """로그용: 앞 200자 (줄바꿈은 \\n으로 한 줄에) + 글자 수."""
    clipped = text[:limit].replace("\n", "\\n")
    more = f", {limit}자까지 표시" if len(text) > limit else ""
    return f"({len(text)}자{more}) {clipped}"


def _plain(text):
    """마크다운 기호가 섞여 나와도 평문으로 (규칙 위반 대비 안전장치)."""
    text = text.replace("**", "")
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.MULTILINE)
    return text.strip()


def _context(case_info, history):
    """매 호출에 넣는 시스템 쪽 컨텍스트 = 케이스 정보 + 케이스 상태(이미 한 접수·이관·조회)."""
    if not case_info:
        return None
    return f"{case.context(case_info)}\n{case_state.render(case_info.get('id'), history)}"


async def _answer(user_key, history, message, case_info, timeout, trace):
    messages = list(history) + [message]
    tc_id = case_info.get("id") if case_info else None
    customer_text = message.get("content", "")
    forced = {}  # 왕복 상한에서 시스템이 접수한 이관 번호
    # 이번 턴에 새로 생긴 이관만 '이번 사안의 이관'으로 본다 (앞 턴의 다른 사안 번호를 잘못 안내하지 않게)
    earlier = {r for _, r, _ in escalation.done_in_case(tc_id)}

    def this_turn_receipt():
        new = [r for _, r, _ in escalation.done_in_case(tc_id) if r not in earlier]
        return new[-1] if new else None

    async def tool_executor(name, args):
        # 같은 대상·레인 이관은 다시 보내지 않고 앞선 결과를 돌려줌
        result = escalation.previous(name, args, tc_id) or await actions.execute(name, args, tc_id)
        store.record_tool_call(user_key, tc_id, name, args, result)
        return result

    async def on_round_cap():
        # 조회를 거듭하다 상한에 닿음 = 이번 턴 안에 끝내지 못한 사안. 말로만 넘기지 않게 이관을 먼저 남긴다
        # 이번 턴에 이미 이관했으면 그 번호, 아니면 새로 접수 (같은 레인·대상이 앞서 있으면 force가 그 번호를 돌려줌)
        receipt = this_turn_receipt() or await escalation.force(
            user_key, tc_id, "도구 호출 왕복 상한에 닿아 처리를 마치지 못함", customer_text)
        if not receipt:
            return None
        forced["receipt"] = receipt
        return (f"[시스템] 도구 호출 한도에 닿아 시스템이 담당자 이관을 접수했다. "
                f"{escalation.receipt_sentence(receipt, case_info)} 확인된 사실과 함께 이 접수번호와 회신 날짜를 고객에게 안내한다.")

    async def fallback(why):
        return await escalation.fallback_reply(user_key=user_key, tc_id=tc_id, case_info=case_info,
                                               customer_text=customer_text, why=why,
                                               this_turn=forced.get("receipt") or this_turn_receipt())

    try:
        reply = await asyncio.wait_for(
            llm.generate(
                messages,
                system=SYSTEM_PROMPT,
                context=_context(case_info, history),
                tools=TOOLS,
                tool_executor=tool_executor if TOOLS else None,
                trace=trace,
                on_round_cap=on_round_cap if TOOLS else None,
            ),
            timeout=max(timeout - ESCALATION_RESERVE, 1),
        )
    except asyncio.TimeoutError:
        logger.warning("답변 생성 시간(수신부터 %.0f초) 초과, 이관을 남기고 답장", REPLY_DEADLINE)
        return await fallback("답변 생성 시간 초과")
    if not reply:
        return await fallback("답변 생성 실패")
    return await escalation.ensure(_plain(reply), user_key=user_key, tc_id=tc_id, case_info=case_info,
                                   customer_text=customer_text, forced_receipt=forced.get("receipt"),
                                   this_turn=this_turn_receipt())


async def _user_message(text):
    """LLM에 넘길 사용자 메시지. 사진·음성은 내려받아 media로 붙임.

    반환: (message, 실패 시 바로 보낼 답장). 내려받기에 실패하면 message는 None.
    """
    text, items = media.extract(text)
    attached = []
    for item in items:
        data = await media.download(item["url"])
        if data is None:
            return None, f"{item['label']}을 확인하지 못했어요. 다시 보내 주시겠어요?"
        attached.append({"mime": item["mime"], "data": data})
    if not attached:
        return {"role": "user", "content": text}, None
    # URL 대신 설명 문구를 넣음 (기록에도 이 형태로 남음)
    labels = "·".join(dict.fromkeys(i["label"] for i in items))
    note = f"(고객이 {labels}을 보냈습니다)"
    return {"role": "user", "content": f"{note}\n{text}" if text else note, "media": attached}, None


async def handle_message(bot_token, d, received_at):
    """received_at: time.perf_counter() 기준 메시지 수신 시각."""
    content = d.get("content") or ""
    callback_token = d.get("callbackToken")
    user_key = user_key_of(d)
    logger.info("수신 user=%s %s", user_key, _preview(content))

    if not callback_token:
        logger.warning("callbackToken 없음, 답장 생략 (user=%s)", user_key)
        return

    # case metadata 블록은 고객 발화가 아니므로 떼어 내고, 고객 문의 부분만 사용자 메시지로
    new_case, text = case.parse(content)
    if user_key is None:
        # 다른 사용자와 섞이지 않도록 기록·케이스를 저장하지 않고 이번 메시지만으로 답함
        logger.warning("botUserKey 없음, 대화 기록·케이스 저장 생략")
        history, case_info = (), new_case
    else:
        if new_case:
            logger.info("새 케이스 user=%s %s (이전 %s), 대화 기록 초기화",
                        user_key, new_case["id"], (store.load_case(user_key) or {}).get("id"))
            store.start_case(user_key, new_case)
        history, case_info = store.load_history(user_key, HISTORY_SIZE), store.load_case(user_key)

    message, reply = await _user_message(text)
    trace = []  # 이번 턴의 도구 호출·결과. 기록에 남겨 다음 턴에 같은 조회·접수를 반복하지 않게 함
    if message is not None:
        # 사진·음성 내려받는 데 쓴 시간을 빼고 남은 시간 안에 답변 생성
        remaining = REPLY_DEADLINE - (time.perf_counter() - received_at)
        reply = await _answer(user_key, history, message, case_info, remaining, trace)
    llm_ms = (time.perf_counter() - received_at) * 1000

    # 답이 나온 뒤에 한 쌍으로 저장 (같은 사용자의 메시지가 동시에 와도 user/assistant 순서 유지)
    # 사진·음성은 bytes째 남겨 다음 질문에서도 다시 볼 수 있게 함. 내려받기 실패한 메시지는 남기지 않음
    if user_key is not None and message is not None:
        store.append_turn(user_key, (case_info or {}).get("id"), message,
                          {"role": "assistant", "content": reply, "trace": trace})

    ok = await kakao.send_reply(bot_token, callback_token, reply)
    elapsed_ms = (time.perf_counter() - received_at) * 1000
    # 실제로 보낸 텍스트 (카카오 500자 제한으로 잘린 뒤)
    sent = reply[:kakao.MAX_TEXT_LENGTH]
    if ok:
        logger.info("응답 완료 user=%s %.0fms (답변 생성 %.0fms, 수신→답장 성공) 답장 %s",
                    user_key, elapsed_ms, llm_ms, _preview(sent))
    else:
        logger.error("응답 실패 user=%s %.0fms 답장 %s", user_key, elapsed_ms, _preview(sent))
