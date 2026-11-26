"""구두 카드: 대화로만 나온 기준(finding)과 봇의 말(utterance)을 카드 추출 단위로 만든다. 권위는 '구두'.

- finding은 수집 쪽이 근거 조각까지 붙여 준 '알아낸 것'이라 그대로 원문 조각처럼 쓴다 (본문 + 근거 문장).
- utterance는 거를 것(자료 링크 알림, 짧은 인사, "모른다"류 거절)을 빼고 봇별로 묶는다.
증분 빌드는 finding·utterance id 단위로 새것만 보낸다.
"""

import re

# 규칙이 담길 수 없는 말
SKIP_UTTERANCE = re.compile(r"^\s*(자료\s*:|https?://)|잘 모르|모르겠|말씀드리기 어렵|알려 드리기 어렵|담당이 아니|답변 중")
MIN_UTTERANCE = 20


def finding_segments(findings):
    """[(단위 안 id, 원문)] — 원문 = 발굴 본문 + 근거 문장 (근거 확인은 이 글 안에서 한다)."""
    out = []
    for f in findings:
        ev = (f.get("evidence") or {}).get("text") or ""
        out.append((f"finding:{f['id']}", f"{f.get('text', '')}\n근거: {ev}".strip()))
    return out


def keep_utterance(u):
    text = (u.get("text") or "").strip()
    return len(text) >= MIN_UTTERANCE and not SKIP_UTTERANCE.search(text)


def utterance_units(utterances):
    """봇별 단위 [(봇, [(utterance id, 원문)])] — 거른 뒤."""
    by_bot = {}
    for u in utterances:
        if keep_utterance(u):
            by_bot.setdefault(u.get("bot") or "?", []).append((f"utt:{u['id']}", u["text"]))
    return sorted(by_bot.items())
