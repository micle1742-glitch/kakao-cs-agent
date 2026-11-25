"""대본대로 답하는 가짜 LLM. llm._create_once 자리에 끼운다.

진짜 Gemini 응답 객체(GenerateContentResponse)를 만들어 돌려주므로 llm.generate의 도구 왕복 루프,
왕복 상한, handler의 시간 제한·이관 보장 코드가 실제 그대로 돈다. LLM 없이 '코드가 지키는 것'을 시험하는 용도.

대본(llm_script)은 케이스 전체에 걸친 단계 목록이고, LLM 호출 때마다 앞에서부터 소비한다.
  - {call: [{name: API_03, args: {...}}, ...]}  도구 호출 응답. 왕복 상한(text_only)에 걸리면 건너뛴다
  - {text: "..."}                               글 답. {last_receipt}는 입력에 보이는 마지막 접수번호로 바뀐다
  - {hang: 30}                                  N초 동안 응답하지 않음 (시간 초과 시험)
  - {assert_input: ["...", ...]}                 이번 호출 입력(시스템·케이스 정보·대화)에 문자열이 모두 있는지 기록
"""

import asyncio
import json
import re

from google.genai import types

RECEIPT = re.compile(r"[A-Z]{2,4}-\d{8}-\d{3}")  # 이관·접수번호 (HO-…, RCV-… 등)


def render_input(system, context, contents):
    """LLM이 보게 될 입력 전체를 글로 펼친다 (assert_input·{last_receipt}용)."""
    out = [system or "", context or ""]
    for c in contents:
        for p in c.parts or ():
            if p.text:
                out.append(p.text)
            if p.function_call:
                out.append(f"{p.function_call.name} {json.dumps(p.function_call.args, ensure_ascii=False)}")
            if p.function_response:
                out.append(f"{p.function_response.name} {json.dumps(p.function_response.response, ensure_ascii=False)}")
    return "\n".join(out)


def _response(parts):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=parts), finish_reason=types.FinishReason.STOP)])


class ScriptedLLM:
    def __init__(self, steps):
        self.steps = list(steps or [])
        self.checks = []  # [(통과 여부, 설명)]
        self.calls = 0

    async def create_once(self, contents, system, tools, context=None, text_only=False):
        self.calls += 1
        seen = render_input(system, context, contents)
        while self.steps:
            step = self.steps.pop(0)
            if "assert_input" in step:
                for s in step["assert_input"]:
                    self.checks.append((s in seen, f"LLM 입력에 「{s}」 있음 ({self.calls}번째 호출)"))
                continue
            if "hang" in step:
                await asyncio.sleep(float(step["hang"]))
                continue
            if "call" in step:
                if text_only:
                    continue  # 도구를 막은 호출: 모델이 도구 대신 글로 답한다고 보고 다음 글 단계로
                return _response([types.Part(function_call=types.FunctionCall(
                    id=f"call-{self.calls}-{i}", name=c["name"], args=c.get("args", {})))
                    for i, c in enumerate(step["call"])])
            if "text" in step:
                receipts = RECEIPT.findall(seen)
                text = step["text"].replace("{last_receipt}", receipts[-1] if receipts else "")
                return _response([types.Part(text=text)])
        return _response([types.Part(text="(대본 끝)")])
