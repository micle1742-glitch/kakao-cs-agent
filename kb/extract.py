"""카드 뽑기. 추출기는 둘: 실제 Gemini, 대본(가짜 — 테스트·CI용, LLM 없이 돈다).

추출 단위(unit) = 문서 하나 또는 구두 묶음 하나
  {"key", "title", "label"(권위 이름), "effectiveDate", "code", "segments": [(조각 id, 원문)], "known_topics": [...]}
추출기는 [{topic, rule, values, basis, evidence, chunkId}]를 돌려준다. 근거 확인은 build가 한다.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import yaml

MAX_SEGMENT_CHARS = 12000  # 한 번에 보내는 원문 글자 수 (넘으면 나눠 보냄)

PROMPT = """너는 고객응대 봇에 넣을 지식 카드를 만든다. 아래 원문 조각들에서 고객 응대에 쓰이는 규칙·기준·수치·예외·절차를 카드로 뽑아라.

규칙
- 카드 하나에 규칙 하나. rule은 고객 응대에 바로 쓸 수 있는 한 문장으로 쓴다.
- evidence는 원문 조각에서 **글자 그대로 복사한** 연속된 문장이다. 바꾸거나 줄이거나 요약하지 않는다. chunkId는 그 조각의 id다.
- topic은 짧은 주제 이름이다(예: "하자 접수 기한"). 아래 '이미 있는 주제'에 같은 뜻이 있으면 그 이름을 글자 그대로 쓴다.
- values에는 규칙 속 수치·기한·금액·비율·시각을 넣는다 (name: 무엇의 값인지, value: 원문 표기 그대로).
- basis에는 적용 기준 시점(예: 주문 시점, 신고 시점, 결제 완료 시각)이 원문에 있을 때만 쓰고, 없으면 빈 문자열.
- 규칙이 없는 조각(인사, 목차, 장식, 표의 개별 데이터 행)은 건너뛴다. 표는 개별 행 대신 표를 읽는 법·판정 기준만 카드로 만든다.
- 원문에 없는 내용을 더하지 않는다. 뽑을 것이 없으면 cards를 빈 목록으로 둔다.

문서: {title}
문서 종류(권위): {label}
문서 번호: {code} / 시행일: {date}
이미 있는 주제: {topics}

원문 조각 (각 줄 맨 앞 [id]가 chunkId):
{segments}
"""

SCHEMA = {
    "type": "object",
    "properties": {"cards": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "rule": {"type": "string"},
            "values": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "value": {"type": "string"}}, "required": ["name", "value"]}},
            "basis": {"type": "string"},
            "evidence": {"type": "string"},
            "chunkId": {"type": "string"},
        },
        "required": ["topic", "rule", "evidence", "chunkId"],
    }}},
    "required": ["cards"],
}


def split_segments(segments, limit=MAX_SEGMENT_CHARS):
    """원문 조각들을 limit 글자 안쪽 묶음으로 나눈다 (조각 하나가 limit보다 길면 그 조각만 한 묶음)."""
    batch, size = [], 0
    for seg in segments:
        if batch and size + len(seg[1]) > limit:
            yield batch
            batch, size = [], 0
        batch.append(seg)
        size += len(seg[1])
    if batch:
        yield batch


def render_prompt(unit, segments):
    return PROMPT.format(
        title=unit.get("title") or "(제목 없음)", label=unit.get("label") or "",
        code=unit.get("code") or "없음", date=unit.get("effectiveDate") or "없음",
        topics=", ".join(unit.get("known_topics") or ()) or "(아직 없음)",
        segments="\n".join(f"[{cid}] {text}" for cid, text in segments))


class ScriptedExtractor:
    """대본 추출기: {unit key: [카드, ...] | {"error": "..."}} — 테스트에서 LLM 대신 쓴다.

    없는 key는 빈 목록. 호출된 key를 self.seen에 남긴다(증분 빌드 시험용).
    """

    def __init__(self, script):
        self.script = script if isinstance(script, dict) else yaml.safe_load(Path(script).read_text(encoding="utf-8"))
        self.seen = []
        self.calls = 0

    async def extract(self, unit):
        self.calls += 1
        self.seen.append(unit["key"])
        answer = self.script.get(unit["key"], [])
        if isinstance(answer, dict) and "error" in answer:
            raise RuntimeError(answer["error"])
        return [dict(c) for c in answer]


class GeminiExtractor:
    """Gemini 구조화 출력으로 카드를 뽑는다. 분당 호출 제한, 한도·과부하(429·503) 재시도."""

    def __init__(self, model=None, rpm=0, thinking_level=None, retries=3):
        from google import genai
        self.client = genai.Client(api_key=os.getenv("LLM_API_KEY"))
        self.model = model or os.getenv("KB_MODEL") or "gemini-flash-lite-latest"
        # 사고 설정: 'off'면 budget 0, 아니면 수준 (flash-lite 계열은 budget 0을 받지 않는다)
        self.thinking = thinking_level or os.getenv("KB_THINKING_LEVEL") or "minimal"
        self.gap = 60.0 / rpm if rpm else 0
        self.retries = retries
        self.calls = 0
        self._last = 0.0

    def _config(self):
        from google.genai import types
        thinking = (types.ThinkingConfig(thinking_budget=0) if self.thinking == "off"
                    else types.ThinkingConfig(thinking_level=self.thinking))
        return types.GenerateContentConfig(response_mime_type="application/json", response_json_schema=SCHEMA,
                                           thinking_config=thinking, max_output_tokens=8192, temperature=0)

    async def _call(self, prompt):
        from google.genai import errors
        for attempt in range(self.retries + 1):
            wait = self._last + self.gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            try:
                resp = await self.client.aio.models.generate_content(model=self.model, contents=prompt,
                                                                     config=self._config())
                return json.loads(resp.text or "{}").get("cards", [])
            except errors.APIError as e:
                if e.code not in (429, 503) or attempt == self.retries:
                    raise
                await asyncio.sleep(20 * (attempt + 1))

    async def extract(self, unit):
        cards = []
        for segments in split_segments(unit["segments"]):
            cards += await self._call(render_prompt(unit, segments))
        return cards
