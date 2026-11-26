"""위기 신호 사전 분류와 고정 응대 흐름.

추론을 꺼 둔 LLM은 "마지막으로 꼭 먹어 보고 싶었어요" 같은 이별 인사형 신호를 흘리거나, 반대로 "먹고 배탈 났어요"를
위기로 잘못 보낼 수 있다. 그래서 LLM보다 먼저 코드가 본다 (rules.yaml의 crisis 정규식).
  crisis  자·타해 표현 또는 이별 인사형 애매한 표현 (애매하면 위기 — fail-closed)
  health  몸 이상 문의(배탈·복통 등). 위기가 아니다 — 위기 표현이 같이 있으면 위기가 이긴다
  none    그 밖
위기면 LLM을 거치지 않고 흐름을 고정한다: 공감 1문장 + 상담 자원 안내(케이스 중 1번) + 주문 건 책임 + 안전 이관.
"""

import re

from app import rules

CRISIS, HEALTH, NONE = "crisis", "health", "none"


def _any(patterns, text):
    return any(re.search(p, text) for p in patterns or ())


def classify(text):
    cfg = rules.get()["crisis"]
    text = " ".join(str(text or "").split())
    if _any(cfg.get("signals"), text) or _any(cfg.get("ambiguous"), text):
        return CRISIS
    if _any(cfg.get("health"), text):
        return HEALTH
    return NONE


def reply(history):
    """고정 응대 문구. 상담 자원 안내는 이 케이스에서 아직 안 했을 때만 넣는다."""
    cfg = rules.get()["crisis"]
    said = "\n".join(m["content"] for m in history if m["role"] == "assistant")
    resource = cfg.get("resource") or ""
    parts = [cfg.get("empathy") or ""]
    if resource and resource not in said:
        parts.append(resource)
    parts.append(cfg.get("order") or "")
    return " ".join(p for p in parts if p)


def escalation_args():
    esc = rules.get()["crisis"].get("escalation") or {}
    return {"target": esc.get("target"), "priority": esc.get("priority"), "lane": esc.get("lane")}
