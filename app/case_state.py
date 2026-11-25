"""케이스 상태: 이번 상담에서 이미 한 일을 시스템 기록에서 뽑아 매 턴 LLM에 짧은 표로 넣는다.

대화 기록만 믿으면 긴 상담에서 앞의 접수·이관을 놓치거나 같은 일을 다시 한다. 도구 호출 기록(sqlite)은
LLM이 아니라 코드가 남긴 것이라 정확하므로, 여기서 '이미 한 일'을 다시 만들어 보여 준다.

- 몇 번째 답인지 (케이스당 응답 한도 관리)
- 접수(상태를 바꾼 호출)와 결과, 이관과 접수번호
- 조회한 것
- 한 번만 안내해야 하는 문구를 이미 몇 번 썼는지 (STATE_TRACK_PHRASES, 쉼표로 구분. 예: 상담 전화번호)
"""

import json
import os

from app import actions, escalation, store

REPLY_LIMIT = int(os.getenv("CASE_REPLY_LIMIT") or "10")
TRACK_PHRASES = [p.strip() for p in (os.getenv("STATE_TRACK_PHRASES") or "").split(",") if p.strip()]
ARGS_PREVIEW = 120


def _short(value, limit=ARGS_PREVIEW):
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + "…"


def _method(name):
    op = actions._operations.get(name)
    return op["method"] if op else None


def render(tc_id, history):
    """<case_state> 블록 문자열. history는 이번 케이스의 대화 기록(handler가 불러온 것)."""
    n_replies = sum(1 for m in history if m["role"] == "assistant")
    lines = ["<case_state>",
             "이번 케이스에서 이미 한 접수·이관과 조회 (시스템 기록이며 고객 발화가 아니다)",
             f"- 이번 답은 이 케이스의 {n_replies + 1}번째 답 (케이스당 {REPLY_LIMIT}건 한도)"]

    escalations = escalation.done_in_case(tc_id)
    for (lane, target), receipt, _ in escalations:
        lines.append(f"- 이관 {lane}/{target or '대상 미기재'} → {receipt} "
                     "(같은 대상·레인으로 다시 이관하지 말고 이 번호와 회신 날짜를 안내한다)")

    lookups = []
    for c in store.load_tool_calls(tc_id):
        name, result = c["name"], c["result"]
        if name == escalation.TOOL or result.get("duplicate"):
            continue
        method = _method(name)
        if method is None:  # 로컬 계산 도구
            continue
        if method == "GET":
            label = f"{name}({_short(c['args'], 60)})"
            if label not in lookups:
                lookups.append(label)
        else:
            outcome = _short(result.get("data")) if result.get("ok") else f"실패 {result.get('status', result.get('error'))}"
            lines.append(f"- 접수 {name} {_short(c['args'])} → {outcome}")
    if lookups:
        lines.append(f"- 조회함: {', '.join(lookups)}")

    said = "\n".join(m["content"] for m in history if m["role"] == "assistant")
    for phrase in TRACK_PHRASES:
        n = said.count(phrase)
        if n:
            lines.append(f"- 「{phrase}」 안내를 이미 {n}번 했다")
    if len(lines) == 3:
        lines.append("- 아직 한 접수·이관·조회 없음")
    lines.append("</case_state>")
    return "\n".join(lines)
