"""함정 회귀 테스트 실행기.

정해 둔 고객 발화(TC의 turns)를 handler에 차례로 넣고, 서버까지 간 액션 API 호출과 답 문구를 규칙으로 채점한다.
액션 API는 항상 가짜(traps/mock_api.py)다. 대회 액션 서버는 대회가 끝나 내려갔다.

사용법
  python run_traps.py                         # traps/cases, 가짜 LLM(대본) — LLM 없이 '코드가 지키는 것'을 시험
  python run_traps.py --llm gemini --cases DIR --knowledge DIR
                                              # 실제 Gemini로 (환경변수 LLM_API_KEY, LLM_MODEL 필요)
  옵션: --only ID ...   --repeat N   --json 결과.json   --spec openapi.yaml   --pause 초

TC 형식은 traps/cases/*.yaml과 traps/scoring.py 설명을 보세요.
종료 코드: 모두 통과 0, 하나라도 실패 1.
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
# 재시도 뒤에도 실패한 실제 LLM 호출 (한도·과부하). 하나라도 있으면 결과에 모델 실패가 섞인 것
LLM_FAILURES = []


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="함정 회귀 테스트")
    p.add_argument("--cases", default=str(BASE_DIR / "traps" / "cases"), help="TC 폴더 (*.yaml)")
    p.add_argument("--llm", choices=["script", "gemini"], default="script")
    p.add_argument("--knowledge", help="시스템 프롬프트에 넣을 knowledge 폴더 (기본: 프로젝트 knowledge/)")
    p.add_argument("--kb", help="kb 카드 폴더 (기본: knowledge/kb/current). kb 빌드가 staging을 시험할 때 씀")
    p.add_argument("--spec", default=str(BASE_DIR / "traps" / "fixtures" / "openapi.yaml"), help="액션 API 명세")
    p.add_argument("--only", nargs="*", help="이 ID의 TC만")
    p.add_argument("--repeat", type=int, default=1, help="TC마다 반복 횟수 (실제 LLM은 답이 매번 달라서)")
    p.add_argument("--pause", type=float, default=0, help="턴 사이 쉬는 시간(초, 무료 키 속도 제한 대비)")
    p.add_argument("--json", help="결과를 JSON으로 저장할 경로")
    p.add_argument("--rpm", type=float, default=0,
                   help="실제 LLM 분당 호출 상한 (무료 키 대비). 0이면 제한 없음")
    p.add_argument("--thinking-level",
                   help="봇의 사고 설정(budget 0)을 이 수준(minimal·low 등)으로 바꿔 측정. budget 0을 받지 않는 모델용")
    return p.parse_args(argv)


def _throttle_llm(rpm, retries=4):
    """실제 LLM 호출을 분당 rpm회 이하로 맞추고, 429·503(한도·과부하)은 기다렸다 다시 부른다.

    측정용 장치다. 봇이 아니라 실행기가 기다리므로, 이 모드에서는 응답 시간이 아니라 정확도만 본다.
    """
    from google.genai import errors

    from app import llm
    original = llm._create_once
    gap = 60.0 / rpm if rpm else 0
    state = {"last": 0.0}

    async def throttled(*a, **kw):
        for attempt in range(retries + 1):
            wait = state["last"] + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            state["last"] = time.monotonic()
            try:
                return await original(*a, **kw)
            except errors.APIError as e:
                if e.code not in (429, 503) or attempt == retries:
                    LLM_FAILURES.append(e.code)
                    raise
                print(f"    (LLM {e.code}, {20 * (attempt + 1)}초 뒤 다시 부름)", file=sys.stderr)
                await asyncio.sleep(20 * (attempt + 1))
    llm._create_once = throttled


def load_cases(folder, only=None):
    cases = []
    for path in sorted(Path(folder).glob("*.yaml")):
        tc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not only or tc["id"] in only:
            cases.append(tc)
    return cases


def _setup(args):
    """app 모듈은 import할 때 설정·프롬프트·도구를 읽으므로, 환경을 먼저 맞추고 순서대로 import한다."""
    os.environ["ENABLE_TOOLS"] = "true"
    os.environ["USE_EXPLICIT_CACHE"] = "false"
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="traps-")  # 매 실행 빈 sqlite
    if args.llm == "script":
        os.environ["LLM_API_KEY"] = "unused"
    elif not os.getenv("LLM_API_KEY"):
        sys.exit("--llm gemini에는 환경변수 LLM_API_KEY가 필요합니다.")
    else:
        # 한도 대기·재시도 때문에 늦어져도 시간 초과로 처리되지 않게 (이 모드는 정확도 측정용)
        os.environ.setdefault("REPLY_DEADLINE", "900")
        os.environ.setdefault("LLM_TIMEOUT", "90")

    if args.kb:
        os.environ["KB_DIR"] = str(Path(args.kb).resolve())
    from app import prompt
    if args.knowledge:
        prompt.KNOWLEDGE_DIR = Path(args.knowledge)
    from app import actions
    spec = yaml.safe_load(Path(args.spec).read_text(encoding="utf-8"))
    actions._load_spec = lambda: spec
    from app import handler  # noqa: F401  (여기서 TOOLS가 만들어짐)
    # 시스템 프롬프트(규칙 + knowledge + kb 카드)가 만들어지는지 미리 확인 (kb 빌드의 게이트가 이걸 본다)
    print(f"시스템 프롬프트 {len(prompt.system_prompt()):,}자", file=sys.stderr)
    if args.llm == "gemini":
        _throttle_llm(args.rpm)
        if args.thinking_level:
            from google.genai import types

            from app import llm
            llm._thinking_config = lambda model: types.ThinkingConfig(thinking_level=args.thinking_level)


def _header(case_id, meta):
    lines = ["case metadata", f"- id: {case_id}", f"- 상담 인입 일시: {meta['received_at']}"]
    if meta.get("channel"):
        lines.append(f"- 인입창구: {meta['channel']}")
    if meta.get("account"):
        lines.append(f"- 인입 계정: {meta['account']}")
    return "\n".join(lines) + "\n\n"


async def run_case(tc, rep, args):
    from app import actions, handler, kakao, llm
    from traps import scoring
    from traps.fake_llm import ScriptedLLM
    from traps.mock_api import MockActionAPI

    case_id = f"{tc['id']}-r{rep}"
    meta = tc["case"]
    api = MockActionAPI(actions._operations, tc.get("api_fixtures"), meta.get("received_at"))
    actions._send = api.send
    scripted = None
    if args.llm == "script":
        scripted = ScriptedLLM(tc.get("llm_script"))
        llm._create_once = scripted.create_once

    current = []

    async def capture(bot_token, callback_token, text):
        current.append(text)
        return True
    kakao.send_reply = capture

    old_deadline = handler.REPLY_DEADLINE
    if "reply_deadline" in tc:
        handler.REPLY_DEADLINE = float(tc["reply_deadline"])
    turn_replies, checks, seconds = [], [], []
    try:
        for i, turn in enumerate(tc["turns"]):
            current = []
            n = len(api.calls)
            text = (_header(case_id, meta) if i == 0 else "") + turn["customer"]
            start = time.perf_counter()
            await handler.handle_message("trap", {"content": text, "callbackToken": "trap",
                                                  "botUserKey": f"trap-{case_id}"}, start)
            seconds.append(round(time.perf_counter() - start, 2))
            turn_replies.append(current)
            if turn.get("expect"):
                checks += scoring.check(turn["expect"], current, api.calls[n:], f"{i + 1}턴")
            if args.pause and i + 1 < len(tc["turns"]):
                await asyncio.sleep(args.pause)
    finally:
        handler.REPLY_DEADLINE = old_deadline
    all_replies = [r for rs in turn_replies for r in rs]
    checks += scoring.check(tc.get("expect") or {}, all_replies, api.calls, "케이스")
    checks += scoring.basic_checks(turn_replies, api.calls)
    if scripted:
        checks += scripted.checks
    return {
        "id": tc["id"], "rep": rep, "title": tc.get("title", ""), "tags": tc.get("tags", []),
        "passed": all(ok for ok, _ in checks),
        "checks": [{"ok": ok, "desc": d} for ok, d in checks],
        "transcript": [{"customer": t["customer"], "bot": r} for t, r in zip(tc["turns"], turn_replies)],
        "calls": [{k: c[k] for k in ("tool", "key", "params", "body", "status", "data")} for c in api.calls],
        "seconds": seconds,
    }


async def main(argv=None):
    args = parse_args(argv)
    _setup(args)
    cases = load_cases(args.cases, args.only)
    if not cases:
        sys.exit(f"TC가 없습니다: {args.cases}")
    results = []
    for tc in cases:
        for rep in range(1, args.repeat + 1):
            r = await run_case(tc, rep, args)
            results.append(r)
            mark = "통과" if r["passed"] else "실패"
            print(f"[{mark}] {r['id']} (반복 {rep}) {r['title']}")
            for c in r["checks"]:
                if not c["ok"]:
                    print(f"    ✗ {c['desc']}")
    n_pass = sum(r["passed"] for r in results)
    n_checks = sum(len(r["checks"]) for r in results)
    n_checks_ok = sum(c["ok"] for r in results for c in r["checks"])
    print(f"\nTC 통과 {n_pass}/{len(results)} ({n_pass / len(results):.0%}) · "
          f"검사 통과 {n_checks_ok}/{n_checks} ({n_checks_ok / max(n_checks, 1):.0%})")
    if LLM_FAILURES:
        print(f"주의: 실제 LLM 호출 {len(LLM_FAILURES)}번이 재시도 뒤에도 실패했습니다 {sorted(set(LLM_FAILURES))}. "
              "모델 실패가 섞인 결과라 비교에 쓰지 마세요.")
    if args.json:
        Path(args.json).write_text(json.dumps({"llm": args.llm, "model": os.getenv("LLM_MODEL"),
                                               "llm_failures": LLM_FAILURES, "results": results},
                                              ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
