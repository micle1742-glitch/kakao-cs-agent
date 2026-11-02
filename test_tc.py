"""공개 TC 하나로 상담 흐름 시험: 초기 대화를 handler에 넣고, 고객 역할은 Gemini가 TC의 "상황"을 보고 연기한다.

사용법: uv run test_tc.py [TC ID] [--turns N] [--verbose] [--json 경로]   (기본 CASE-000, 최대 6턴)
       uv run test_tc.py --all   (knowledge/tc/ 전부, TC마다 별도 프로세스·reset, 결과는 test_results/)

- 실제 액션 API(연습 호출)를 쓴다. 끝나면 reset.py로 액션 데이터를 초기화한다.
- 고객 역할은 자기 계정 정보(이름으로 회원 검색 → 주문 목록)를 알고 시작한다. 이 조회도 API 호출 수에 들어간다.
- 출력: 대화 기록, 턴별 응답 시간, 말풍선 수·평균 글자 수, 호출한 API와 "기대 대응" API 비교
"""

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# 시험은 항상 액션 API 도구를 켜고 돌림 (.env의 값보다 우선)
os.environ["ENABLE_TOOLS"] = "true"
load_dotenv()

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from app import actions, case, handler, kakao, llm, media  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
TC_DIR = BASE_DIR / "knowledge" / "tc"
# 자체 제작 TC (--all에는 포함하지 않음). 액션 서버는 공개 TC ID만 받으므로 case metadata의 id는 공개 TC ID를 쓴다
CUSTOM_TC_DIR = BASE_DIR / "knowledge" / "tc_custom"
END = "[END]"


def load_tc(tc_id):
    path = TC_DIR / f"{tc_id}.md"
    if not path.exists():
        path = CUSTOM_TC_DIR / f"{tc_id}.md"
    text = path.read_text(encoding="utf-8")

    def section(title, level):
        m = re.search(rf"^{'#' * level} {title}\s*\n(.*?)(?=^#{{1,{level}}} |\Z)", text, re.M | re.S)
        return m.group(1).strip() if m else ""

    checks = re.search(r"```json\s*(.*?)```", section("검사", 6), re.S)
    return {
        "initial": section("초기 대화", 4),
        "inquiry": section("고객 문의", 6),
        "situation": section("상황", 6),
        "expected": section("기대 대응", 6),
        "checks": json.loads(checks.group(1)) if checks else None,
    }


def run_checks(spec, transcript, calls):
    """자체 TC의 ###### 검사 블록 실행. [(통과 여부, 설명), ...]"""
    bot = [t for who, t in transcript if who == "상담원"]
    results = []
    trigger = spec.get("trigger")
    crisis_reply = None
    if trigger:
        # 트리거 발화가 나온 고객 메시지 바로 다음 상담원 답
        for i, (who, text) in enumerate(transcript):
            if who == "고객" and trigger in text and i + 1 < len(transcript):
                crisis_reply = transcript[i + 1][1]
                break
        results.append((crisis_reply is not None, f"고객이 트리거 발화 「{trigger}」를 보냄"))
    for phrase in spec.get("crisis_turn_contains", []):
        results.append((bool(crisis_reply) and phrase in crisis_reply, f"위기 턴 답에 「{phrase}」 포함"))
    for phrase in spec.get("must_not_contain", []):
        hit = [t for t in bot if phrase in t]
        results.append((not hit, f"금지 표현 「{phrase}」 없음" + (f" — 발견: {hit[0][:60]}" if hit else "")))
    if spec.get("resource_once"):
        n = sum(t.count(spec["resource_once"]) for t in bot)
        results.append((n == 1, f"「{spec['resource_once']}」 안내 정확히 1번 (실제 {n}번)"))
    escalations = [c for c in calls if c["api"] == "API-35"]
    if spec.get("escalation"):
        want = spec["escalation"]
        ok = [c for c in escalations if c["args"].get("lane") == want["lane"]
              and str(c["args"].get("reason", "")).startswith(want["reason_prefix"])]
        results.append((bool(ok), f"API-35 lane={want['lane']}, reason이 「{want['reason_prefix']}」로 시작"))
    if spec.get("no_escalation_prefix"):
        bad = [c for c in escalations if str(c["args"].get("reason", "")).startswith(spec["no_escalation_prefix"])]
        results.append((not bad, f"「{spec['no_escalation_prefix']}」 이관 없음"))
    return results


def expected_apis(expected):
    """기대 대응에서 API 번호를 처음 나온 순서대로 (같은 단계 번호와 함께)."""
    found = {}
    for line in expected.splitlines():
        step = re.match(r"\s*(\d+)\.", line)
        for api in re.findall(r"API-\d+", line):
            found.setdefault(api, step.group(1) if step else "?")
    return found


async def customer_profile(tc_id, account):
    """고객 역할이 알고 있을 자기 정보: 인입 계정 이름으로 회원 검색 → 회원별 주문 목록."""
    parts = [p.strip() for p in (account or "").split("·")]
    name = parts[1] if len(parts) > 1 else None
    if not name:
        return "(계정 정보 없음)"
    profile = {}
    resp = await actions._send("GET", "/api/members", tc_id, {"name": name})
    members = resp.json() if resp.is_success else []
    profile["회원"] = members
    items = members if isinstance(members, list) else members.get("items", [members])
    for m in items[:2]:
        member_id = m.get("member_id") if isinstance(m, dict) else None
        if member_id:
            resp = await actions._send("GET", "/api/orders", tc_id, {"member_id": member_id})
            profile[f"{member_id} 주문"] = resp.json() if resp.is_success else resp.text
    return json.dumps(profile, ensure_ascii=False)[:6000]


class Customer:
    """TC 상황을 연기하는 고객 (Gemini)."""

    def __init__(self, tc, profile):
        self.client = genai.Client(api_key=os.getenv("LLM_API_KEY"))
        self.model = os.getenv("LLM_MODEL", "gemini-flash-latest")
        self.system = f"""너는 「교동마을 산지직송」 조합몰의 고객이다. 카카오톡으로 상담원(챗봇)과 대화한다.

상황(너만 아는 사실이며 상담원은 모른다):
{tc['situation']}

네가 처음 보낸 문의:
{tc['inquiry']}

네 계정에서 볼 수 있는 정보(주문번호 등은 여기서 상황에 맞는 것을 골라 말한다):
{profile}

규칙
- 상황에 적힌 성격·반응대로만 행동하고, 상황에 없는 새 요구나 사실을 지어내지 않는다.
- 상담원이 묻는 것에 답한다. 한 번에 1~2문장, 카톡 말투로 쓴다. 마크다운을 쓰지 않는다.
- 출력은 고객이 카톡으로 보낼 메시지 그 자체만 쓴다. 생각·설명·지시문을 붙이지 않는다.
- 상담원이 요청을 접수했거나 안내를 마쳐 더 물을 것이 없으면 다른 말 없이 {END} 만 출력한다."""

    async def reply(self, transcript):
        # 고객 시점: 상담원 말이 user, 내 말이 model
        contents = [
            types.Content(role="model" if who == "고객" else "user", parts=[types.Part(text=text)])
            for who, text in transcript
        ]
        resp = await self.client.aio.models.generate_content(
            model=self.model, contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=self.system, max_output_tokens=300,
                thinking_config=types.ThinkingConfig(thinking_level="low"),
            ),
        )
        return (resp.text or END).strip()


async def run(tc_id, max_turns):
    tc = load_tc(tc_id)
    case_info, first_text = case.parse(tc["initial"])
    user_key = f"test-{tc_id}"

    # 봇이 부른 도구 기록 (GET 캐시에 걸려도 기록되도록 execute를 감쌈)
    calls = []
    original_execute = actions.execute

    async def recording_execute(name, args, tc):
        result = await original_execute(name, args, tc)
        calls.append({"api": name.replace("_", "-") if name.startswith("API_") else name, "args": args, "status": result.get("status", result.get("error"))})
        return result
    actions.execute = recording_execute

    replies = []

    async def capture(bot_token, callback_token, text):
        replies.append(text)
        return True
    kakao.send_reply = capture

    # LLM 호출마다 입력 토큰 기록 (턴별 도구 왕복 수 = 그 턴의 LLM 호출 수)
    llm_calls = []
    original_record = llm._record_usage

    def recording_usage(model, usage, elapsed_ms):
        llm_calls.append({"input": (usage.prompt_token_count or 0) + (usage.tool_use_prompt_token_count or 0),
                          "cache": usage.cached_content_token_count or 0, "ms": elapsed_ms})
        original_record(model, usage, elapsed_ms)
    llm._record_usage = recording_usage

    print(f"=== {tc_id} ({case_info['received_at']}) 고객 계정 정보 조회 중...")
    profile = await customer_profile(tc_id, case_info.get("account"))
    customer = Customer(tc, profile)

    transcript = [("고객", first_text or "(사진·음성만 보냄)")]
    turns = []
    message = tc["initial"]  # 첫 턴은 case metadata 포함 원문 그대로
    for turn in range(1, max_turns + 1):
        n, n_llm, n_calls, n_retry = len(replies), len(llm_calls), len(calls), llm.retry_count
        start = time.perf_counter()
        await handler.handle_message("test", {"content": message, "callbackToken": "test", "botUserKey": user_key}, start)
        elapsed = time.perf_counter() - start
        bot = "\n".join(replies[n:]) or "(답장 없음)"
        this_llm = llm_calls[n_llm:]
        info = {
            "turn": turn, "seconds": round(elapsed, 1), "bubbles": len(replies) - n,
            "llm_rounds": len(this_llm), "tools": [c["api"] for c in calls[n_calls:]],
            "max_input_tokens": max((c["input"] for c in this_llm), default=0),
            "llm_ms": [c["ms"] for c in this_llm],
            "fallback": any(_is_fallback(r) for r in replies[n:]),
            "retries": llm.retry_count - n_retry,
        }
        turns.append(info)
        print(f"\n[{turn}턴] 고객: {transcript[-1][1]}")
        print(f"[{turn}턴] 상담원 ({elapsed:.1f}초, {len(bot)}자, LLM {info['llm_rounds']}회 "
              f"{info['llm_ms']}ms, 재시도 {info['retries']}회, 입력 최대 {info['max_input_tokens']}토큰, 도구 {info['tools']}): {bot}")
        transcript.append(("상담원", bot))
        if turn == max_turns:
            break
        message = await customer.reply(transcript)
        if END in message:
            print(f"\n(고객 대화 종료: {turn}턴)")
            break
        transcript.append(("고객", message))

    # --- 요약 ---
    sizes = [len(r) for r in replies]
    print("\n=== 응답 통계")
    print(f"말풍선 {len(replies)}개, 평균 {sum(sizes) / max(len(sizes), 1):.0f}자 (최대 {max(sizes, default=0)}자)")
    print("턴별 응답 시간: " + ", ".join(f"{t['turn']}턴 {t['seconds']:.1f}초" for t in turns))

    expected = expected_apis(tc["expected"])
    called = list(dict.fromkeys(c["api"] for c in calls))
    print("\n=== API 비교 (기대 대응 ↔ 실제 호출)")
    print(f"{'기대(단계)':<16}{'호출':<10}")
    for api in sorted(set(expected) | set(called), key=lambda a: (not a.startswith("API-"), int(a.split("-")[1]) if a.startswith("API-") else 0, a)):
        exp = f"{api} ({expected[api]})" if api in expected else "-"
        got = api if api in called else "-"
        mark = "✓" if api in expected and api in called else ("✗ 누락" if api in expected else "＋ 추가")
        print(f"{exp:<16}{got:<10}{mark}")
    print("\n호출 상세:")
    for c in calls:
        print(f"  {c['api']} {json.dumps(c['args'], ensure_ascii=False)} -> {c['status']}")
    if not calls:
        print("  (없음)")

    check_results = run_checks(tc["checks"], transcript, calls) if tc["checks"] else []
    if check_results:
        print("\n=== 검사")
        for ok, label in check_results:
            print(f"  {'✓' if ok else '✗'} {label}")
        print(f"  → {sum(ok for ok, _ in check_results)}/{len(check_results)} 통과")

    return {
        "checks": [{"ok": ok, "label": label} for ok, label in check_results],
        "tc_id": tc_id,
        "expected": list(expected),
        "called": [c["api"] for c in calls],
        "calls": calls,
        "bubbles": len(replies),
        "avg_chars": round(sum(sizes) / max(len(sizes), 1)),
        "max_seconds": max((t["seconds"] for t in turns), default=0),
        "fallbacks": sum(_is_fallback(r) for r in replies),
        "retries": sum(t["retries"] for t in turns),
        "turns": turns,
        "transcript": transcript,
    }


def _is_fallback(text):
    return text == handler.FALLBACK_REPLY or text.endswith("확인하지 못했어요. 다시 보내 주시겠어요?")


def _short(api):
    """표 표시용: API-11 → 11, 로컬 계산 도구는 이름 그대로."""
    return api[4:] if api.startswith("API-") else api


def run_all(max_turns):
    """TC 전부를 하나씩 별도 프로세스로 실행 (각 실행이 끝나면 reset.py로 초기화). 결과 표와 results.json."""
    out_dir = BASE_DIR / "test_results"
    out_dir.mkdir(exist_ok=True)
    results = []
    for path in sorted(TC_DIR.glob("*.md")):
        tc_id = path.stem
        json_path = out_dir / f"{tc_id}.json"
        print(f"--- {tc_id} 실행 중...", flush=True)
        with (out_dir / f"{tc_id}.log").open("w", encoding="utf-8") as log:
            subprocess.run([sys.executable, str(Path(__file__)), tc_id, "--turns", str(max_turns),
                            "--json", str(json_path)], stdout=log, stderr=subprocess.STDOUT,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if json_path.exists():
            results.append(json.loads(json_path.read_text(encoding="utf-8")))
        else:
            results.append({"tc_id": tc_id, "error": True})
            print(f"    실패 (로그: {out_dir / (tc_id + '.log')})")
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n| TC | 기대 API 순서 | 실제 호출 | 추가 / 누락 | 말풍선 | 평균 글자 | 최대 응답(초) | 재시도 | 고정 문구 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        if r.get("error"):
            print(f"| {r['tc_id']} | 실행 실패 | | | | | | | |")
            continue
        exp, called = r["expected"], list(dict.fromkeys(r["called"]))
        extra = [a for a in called if a not in exp]
        missing = [a for a in exp if a not in called]
        diff = " ".join([f"+{_short(a)}" for a in extra] + [f"−{_short(a)}" for a in missing]) or "일치"
        print(f"| {r['tc_id']} | {' '.join(_short(a) for a in exp) or '-'} | {' '.join(_short(a) for a in r['called']) or '-'} "
              f"| {diff} | {r['bubbles']} | {r['avg_chars']} | {r['max_seconds']} | {r.get('retries', 0)} | {r['fallbacks']} |")

    print("\n8초 넘는 턴:")
    for r in results:
        for t in r.get("turns", []):
            if t["seconds"] > 8:
                print(f"  {r['tc_id']} {t['turn']}턴 {t['seconds']}초: LLM {t['llm_rounds']}회 {t['llm_ms']}ms, "
                      f"입력 최대 {t['max_input_tokens']}토큰, 도구 {t['tools']}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("tc_id", nargs="?", default="CASE-000")
    parser.add_argument("--turns", type=int, default=6)
    parser.add_argument("--verbose", action="store_true", help="handler·actions 로그도 출력")
    parser.add_argument("--json", help="결과를 이 경로에 JSON으로 저장")
    parser.add_argument("--all", action="store_true", help="knowledge/tc/의 TC 전부 실행하고 결과 표 출력")
    a = parser.parse_args()
    if a.all:
        run_all(a.turns)
        return
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        result = await run(a.tc_id, a.turns)
        if a.json:
            Path(a.json).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        await actions.close()
        await media.close()
        print("\n=== reset.py 실행 (액션 데이터 초기화)")
        result = subprocess.run([sys.executable, str(BASE_DIR / "reset.py")], capture_output=True, text=True,
                                encoding="utf-8")
        print(result.stdout.strip() or result.stderr.strip()[-500:])


if __name__ == "__main__":
    asyncio.run(main())
