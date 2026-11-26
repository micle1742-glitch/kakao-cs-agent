"""kb 빌드 명령: 수집 쪽이 내보낸 kb.jsonl → 규칙 카드 → 회귀 테스트 → 봇이 다시 읽음.

사용법
  python build_kb.py --in kb.jsonl                        # 분류만 (LLM 없음). classification.md로 분류 확인
  python build_kb.py --in kb.jsonl --llm gemini --rpm 8   # 새 문서만 카드 뽑기 (LLM_API_KEY 필요)
  python build_kb.py --in kb.jsonl --llm script --script 대본.yaml   # 가짜 추출기 (테스트)
  옵션: --out 폴더(기본 knowledge/kb)  --max-units N  --no-gate  --traps-args "--llm gemini ..."

대회 중 흐름: 수집 쪽 export → 이 명령(30분마다) → classification.md·conflicts.md 확인
  → overrides.yaml·decisions.yaml 고치기 → 이 명령 다시 (LLM 없이 분류·판정만 반영됨)
"""

import argparse
import asyncio
import shlex
import sys
from pathlib import Path

from dotenv import load_dotenv

from kb import build as kb_build
from kb.extract import GeminiExtractor, ScriptedExtractor

BASE_DIR = Path(__file__).resolve().parent


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="kb 빌드")
    p.add_argument("--in", dest="src", required=True, help="kb.jsonl 경로")
    p.add_argument("--out", default=str(BASE_DIR / "knowledge" / "kb"))
    p.add_argument("--llm", choices=["none", "gemini", "script"], default="none")
    p.add_argument("--script", help="--llm script의 대본 (YAML: 단위 key → 카드 목록)")
    p.add_argument("--model", help="카드 뽑을 모델 (기본 KB_MODEL 또는 gemini-flash-lite-latest)")
    p.add_argument("--rpm", type=float, default=0, help="분당 LLM 호출 상한")
    p.add_argument("--max-units", type=int, help="이번에 처리할 최대 단위 수 (한도 아끼기)")
    p.add_argument("--no-gate", action="store_true", help="회귀 테스트 없이 공개 (위험: 시험용)")
    p.add_argument("--traps-args", default="", help="게이트의 run_traps.py에 넘길 인자 (기본: 가짜 LLM 예시 TC)")
    return p.parse_args(argv)


def main(argv=None):
    load_dotenv()
    args = parse_args(argv)
    if args.llm == "gemini":
        extractor = GeminiExtractor(model=args.model, rpm=args.rpm)
    elif args.llm == "script":
        if not args.script:
            sys.exit("--llm script에는 --script가 필요합니다.")
        extractor = ScriptedExtractor(args.script)
    else:
        extractor = None
    traps_args = shlex.split(args.traps_args)
    gate = None if args.no_gate else (lambda staging: kb_build.default_gate(staging, traps_args))
    report = asyncio.run(kb_build.build(args.src, args.out, extractor, gate=gate, max_units=args.max_units))
    print(f"\n카드 {report['cards']}장 · 충돌 {report['conflicts']}건 · 남은 단위 {report['pendingUnits']}개 · "
          f"공개 {'예' if report['published'] else '아니오'} ({report['gate']})")
    print(f"검토 파일: {Path(args.out) / 'classification.md'}, {Path(args.out) / 'conflicts.md'}, "
          f"{Path(args.out) / 'report.md'}")
    return 0 if report["published"] else 1


if __name__ == "__main__":
    sys.exit(main())
