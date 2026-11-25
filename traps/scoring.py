"""함정 TC 채점 (LLM 없이 규칙으로만).

expect (케이스 전체) 또는 turns[i].expect (그 턴만)에 쓸 수 있는 검사:
  api:          [{tool, args: {필드: 값}, key: 경로변수값, reason_prefix: "[GENERAL"}]  성공(2xx)한 호출 중 모두 맞는 것이 있어야 함
  api_max:      {도구: N}            서버까지 간 호출이 N번 이하 (중복 이관 방지 확인)
  no_api:       [도구, ...]          부르면 안 됨
  contains:     [문자열, ...]        답(말풍선들)에 모두 있어야 함
  contains_any: [[a, b], ...]        묶음마다 하나 이상 있어야 함 (예: 날짜 표기 여러 가지)
  not_contains: [문자열, ...]        답 어디에도 없어야 함 (금지 문구)
기본 검사(항상): 말풍선마다 500자 이하, 턴마다 말풍선 1개.
"""

MAX_CHARS = 500


def _match(call, want):
    if call["tool"] != want["tool"] or not 200 <= call["status"] < 300:
        return False
    sent = {**(call.get("params") or {}), **(call.get("body") or {})}
    for k, v in (want.get("args") or {}).items():
        if str(sent.get(k)) != str(v):
            return False
    if "key" in want and str(call.get("key")) != str(want["key"]):
        return False
    if "reason_prefix" in want and not str(sent.get("reason", "")).startswith(want["reason_prefix"]):
        return False
    return True


def _describe(want):
    extra = ", ".join(f"{k}={v}" for k, v in (want.get("args") or {}).items())
    if "key" in want:
        extra = f"{want['key']}" + (f", {extra}" if extra else "")
    if "reason_prefix" in want:
        extra += f", reason이 「{want['reason_prefix']}」로 시작"
    return f"{want['tool']}({extra.strip(', ')})"


def check(expect, replies, calls, scope):
    """[(통과 여부, 설명)]. scope는 설명 앞에 붙는 말 ('케이스' 또는 'N턴')."""
    results = []
    text = "\n".join(replies)
    for want in expect.get("api", ()):
        results.append((any(_match(c, want) for c in calls), f"[{scope}] 호출 {_describe(want)}"))
    for tool, n in (expect.get("api_max") or {}).items():
        got = sum(1 for c in calls if c["tool"] == tool)
        results.append((got <= n, f"[{scope}] {tool} {n}번 이하 (실제 {got}번)"))
    for tool in expect.get("no_api", ()):
        got = sum(1 for c in calls if c["tool"] == tool)
        results.append((got == 0, f"[{scope}] {tool} 부르지 않음 (실제 {got}번)"))
    for s in expect.get("contains", ()):
        results.append((s in text, f"[{scope}] 답에 「{s}」"))
    for group in expect.get("contains_any", ()):
        results.append((any(s in text for s in group), f"[{scope}] 답에 「{' / '.join(group)}」 중 하나"))
    for s in expect.get("not_contains", ()):
        results.append((s not in text, f"[{scope}] 답에 「{s}」 없음"))
    return results


def basic_checks(turn_replies):
    results = []
    for i, replies in enumerate(turn_replies, 1):
        results.append((len(replies) == 1, f"[{i}턴] 말풍선 1개 (실제 {len(replies)}개)"))
        for r in replies:
            if len(r) > MAX_CHARS:
                results.append((False, f"[{i}턴] 말풍선 {MAX_CHARS}자 이하 (실제 {len(r)}자)"))
    return results
