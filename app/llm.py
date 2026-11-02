"""LLM(Gemini) 호출 래퍼. generate(messages, system, tools, tool_executor) -> str | None"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime

import httpx
from google import genai
from google.genai import errors, types

from app import store

USAGE_LOG = store.DATA_DIR / "usage.log"
MAX_TOOL_ROUNDS = 4  # 도구 호출 왕복 상한 (응답 속도 보호). 넘으면 도구 없이 지금까지 결과로 답함
DEFAULT_MODEL = "gemini-flash-latest"  # 최신 Flash 계열 별칭
CACHE_TTL = int(os.getenv("LLM_CACHE_TTL", "3600"))  # 명시적 캐시 유지 시간(초)
# 명시적 캐시 사용 여부. 무료 키는 캐시 생성이 429로 막히므로 기본 false, 유료 키면 true로
USE_EXPLICIT_CACHE = os.getenv("USE_EXPLICIT_CACHE", "false").lower() == "true"

# LLM 호출 1회 제한 시간(초). 넘으면 같은 요청을 CALL_RETRIES번 다시 보내고, 그래도 넘으면 실패(고정 문구)
CALL_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "15"))
CALL_RETRIES = int(os.getenv("LLM_RETRIES", "1"))
# 비상용 키: 주 키가 크레딧 소진·결제 오류를 내면 그 뒤로는 이 키로 부름 (무료 키라 캐시 없이, 제한 시간 넉넉히).
# 프로세스를 다시 시작하면 주 키부터 다시 시도한다 (402는 1초 안에 와서 전환 비용이 작음)
FALLBACK_KEY = os.getenv("LLM_API_KEY_FALLBACK", "").strip()
FALLBACK_TIMEOUT = float(os.getenv("LLM_FALLBACK_TIMEOUT", "25"))
# 프로세스 시작 뒤 시간 초과로 재시도한 횟수 (test_tc.py가 턴별 재시도 수를 셀 때 씀)
retry_count = 0

logger = logging.getLogger("llm")

_client = None
# 명시적 캐시: (model, system, tools) 키별 {"name", "expires"}
_caches = {}
_cache_lock = asyncio.Lock()
# 캐시 생성이 한 번이라도 실패하면 이 프로세스(세션) 동안은 다시 시도하지 않음
_cache_disabled = not USE_EXPLICIT_CACHE
# 비상용 키로 전환했는지
_using_fallback = False


def _model():
    return os.getenv("LLM_MODEL", DEFAULT_MODEL)


def _call_timeout():
    return FALLBACK_TIMEOUT if _using_fallback else CALL_TIMEOUT


def _get_client():
    global _client
    if _client is None:
        # SDK 자체 재시도는 끄고(attempts=1) 호출 시간 제한·재시도는 _generate()에서 직접 한다.
        # HTTP 타임아웃은 그보다 넉넉히 둬서 asyncio 쪽 제한이 먼저 걸리게 함 (단위 ms)
        _client = genai.Client(
            api_key=FALLBACK_KEY if _using_fallback else os.getenv("LLM_API_KEY"),
            http_options=types.HttpOptions(
                timeout=int((_call_timeout() + 5) * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
    return _client


def _is_billing_error(e):
    """크레딧 소진(402)·결제 문제. 429는 결제 문구가 섞여 와도 속도 제한이라 제외."""
    if e.code == 402:
        return True
    message = (e.message or "").lower()
    return e.code in (400, 403) and any(w in message for w in ("billing", "credit", "prepay", "payment"))


def _switch_to_fallback(e):
    """주 키 결제 오류 → 비상용 키로 전환. 전환했으면 True."""
    global _client, _using_fallback, _cache_disabled
    if _using_fallback or not FALLBACK_KEY:
        if not FALLBACK_KEY:
            logger.error("주 키 결제 오류인데 LLM_API_KEY_FALLBACK이 비어 있어 전환하지 못함")
        return False
    _using_fallback = True
    _cache_disabled = True  # 명시적 캐시는 주 키 소유라 비상용 키로는 못 씀
    _caches.clear()
    _client = None
    logger.warning(
        "LLM 키 전환: %s 주 키 결제 오류(%s %s) → 비상용 키 사용 (캐시 없음, 제한 시간 %.0f초, 재시도 없음)",
        datetime.now().isoformat(timespec="seconds"), e.code, (e.message or "")[:80], FALLBACK_TIMEOUT,
    )
    return True


def _thinking_config(model):
    # 응답 속도가 중요하므로 사고를 끔(budget 0). gemini-3.8-flash는 thinking_level MINIMAL을 받지 않고
    # LOW가 최저라, 한 단계 더 낮추려면 budget 0을 쓴다 (2.x·3.x 모두 받음)
    return types.ThinkingConfig(thinking_budget=0)


def _load_totals():
    """usage.log 마지막 줄에서 누적 토큰을 복원 (재시작 후에도 이어서 누적)."""
    try:
        last = USAGE_LOG.read_text(encoding="utf-8").strip().splitlines()[-1]
        record = json.loads(last)
        return record["total_input_tokens"], record["total_output_tokens"]
    except (FileNotFoundError, IndexError, ValueError, KeyError):
        return 0, 0


def _record_usage(model, usage, elapsed_ms):
    # prompt_token_count는 캐시 읽기분을 포함한 전체 입력, 출력은 답변 + 사고 토큰
    input_tokens = (usage.prompt_token_count or 0) + (usage.tool_use_prompt_token_count or 0)
    cache_read = usage.cached_content_token_count or 0
    output_tokens = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
    # 이벤트 루프 안에서만 호출되므로 별도 lock 불필요
    total_in, total_out = _load_totals()
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read,
        "output_tokens": output_tokens,
        "elapsed_ms": elapsed_ms,
        "total_input_tokens": total_in + input_tokens,
        "total_output_tokens": total_out + output_tokens,
    }
    with USAGE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.info(
        "LLM %dms tokens: input=%d (cache read %d) output=%d (total input=%d output=%d)",
        elapsed_ms, input_tokens, cache_read, output_tokens,
        record["total_input_tokens"], record["total_output_tokens"],
    )


def _to_gemini_tools(tools):
    """actions.TOOLS 형식({name, description, input_schema})을 Gemini 함수 선언으로."""
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name=t["name"], description=t["description"], parameters_json_schema=t["input_schema"],
        )
        for t in tools
    ])]


# 도구 선언은 두고 호출만 막는 설정 (도구 왕복 상한에 닿은 마지막 호출용)
NO_TOOL_CALLS = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="NONE"))


async def _get_cache(model, system, tools, no_tool_calls=False):
    """시스템 프롬프트(+도구)를 명시적 캐시로 만들어 이름을 반환. 실패하면 None (암시적 캐싱에 맡김).

    knowledge 전체가 들어간 긴 시스템 프롬프트가 매 호출 같으므로 캐시해 두면 입력 토큰 비용과 지연이 준다.
    no_tool_calls: 캐시를 쓰는 요청에는 tool_config를 넣을 수 없어서, 호출을 막은 설정을 담은 캐시를 따로 만든다.
    이 캐시는 도구 왕복 상한에 실제로 닿았을 때 처음 만든다.
    """
    if _cache_disabled:
        return None
    key = (model, system, json.dumps(tools, sort_keys=True, ensure_ascii=False) if tools else None, no_tool_calls)
    # 첫 메시지가 몰려도 캐시는 하나만 만들도록
    async with _cache_lock:
        return await _get_or_create_cache(key, model, system, tools, no_tool_calls)


async def _get_or_create_cache(key, model, system, tools, no_tool_calls):
    global _cache_disabled
    if _cache_disabled:  # 락을 기다리는 동안 다른 호출이 실패했을 수 있음
        return None
    cache = _caches.get(key)
    if cache and cache["expires"] - time.time() > 60:
        return cache["name"]
    try:
        cache = await _get_client().aio.caches.create(
            model=model,
            config=types.CreateCachedContentConfig(
                system_instruction=system,
                tools=_to_gemini_tools(tools) if tools else None,
                tool_config=NO_TOOL_CALLS if no_tool_calls else None,
                ttl=f"{CACHE_TTL}s",
            ),
        )
    except Exception as e:  # 무료 키 429, 최소 토큰 수 미달, 모델 미지원 등
        if no_tool_calls:
            # 보조 캐시만 실패: 이번 호출만 캐시 없이 (기본 캐시는 그대로 씀)
            logger.warning("명시적 캐시(호출 막음) 생성 실패, 이번 호출은 캐시 없이: %s", e)
            return None
        logger.warning("명시적 캐시 생성 실패, 이번 세션은 캐시 없이 호출: %s", e)
        _cache_disabled = True
        _caches.clear()
        return None
    _caches[key] = {"name": cache.name, "expires": time.time() + CACHE_TTL}
    logger.info("명시적 캐시 생성 %s (TTL %ds%s)", cache.name, CACHE_TTL, ", 도구 호출 막음" if no_tool_calls else "")
    return cache.name


async def _generate(model, contents, config):
    """generate_content 1회 + 시간 초과 시 같은 요청 재시도.

    LLM 호출만 다시 보낸다. 도구(액션 API)는 응답을 받은 뒤 generate()에서 실행하므로, 시간 초과로 응답을
    못 받은 호출에는 실행된 도구가 없고 재시도해도 POST가 다시 나가지 않는다. 이미 실행한 도구는 contents에
    결과까지 들어가 있어 모델이 다시 부르지 않으며, 같은 접수는 actions의 중복 방지에도 걸린다.
    """
    global retry_count
    # 비상용 키는 제한 시간이 길어 재시도하면 답장 마감(REPLY_DEADLINE)을 넘기므로 재시도 없음
    retries = 0 if _using_fallback else CALL_RETRIES
    timeout = _call_timeout()
    for attempt in range(retries + 1):
        try:
            return await asyncio.wait_for(
                _get_client().aio.models.generate_content(model=model, contents=contents, config=config),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, httpx.TimeoutException):
            if attempt == retries:
                raise
            retry_count += 1
            logger.warning("LLM 호출 %.0f초 초과, 같은 요청 재시도 (%d/%d)", timeout, attempt + 1, retries)


def _drop_cache(cache_name):
    for key, cache in list(_caches.items()):
        if cache and cache["name"] == cache_name:
            del _caches[key]


async def _create(contents, system, tools, context=None, text_only=False):
    """text_only: 도구 호출 없이 텍스트 답만 받음 (도구 왕복 상한에 닿았을 때).

    주 키가 결제 오류를 내면 비상용 키로 바꿔 같은 호출을 바로 다시 보낸다 (이번 메시지도 답하도록).
    """
    try:
        return await _create_once(contents, system, tools, context, text_only)
    except errors.APIError as e:
        if not (_is_billing_error(e) and _switch_to_fallback(e)):
            raise
    return await _create_once(contents, system, tools, context, text_only)


async def _create_once(contents, system, tools, context=None, text_only=False):
    model = _model()
    config = {
        "max_output_tokens": 4096,
        "thinking_config": _thinking_config(model),
        # 도구는 SDK가 자동 실행하지 않고 아래 generate()의 루프에서 직접 실행
        "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
    }
    # 대화에 도구 호출 기록이 있으므로 마지막 호출은 도구 선언은 두고 호출만 막음
    no_tool_calls = bool(text_only and tools)
    # 케이스 정보(context)는 케이스마다 달라서 캐시에 넣지 않음:
    # 캐시 없이 부를 땐 고정 프롬프트 뒤에 붙이고(앞부분이 같아 암시적 캐싱 유지), 캐시를 쓸 땐 대화 맨 앞에 넣음
    cache_name = await _get_cache(model, system, tools, no_tool_calls) if system else None
    request_contents = contents
    if cache_name:
        config["cached_content"] = cache_name  # 호출 막음 설정은 캐시에 들어 있음
        request_contents = _with_context(contents, context)
    else:
        config["system_instruction"] = _join(system, context)
        config["tools"] = _to_gemini_tools(tools) if tools else None
        if no_tool_calls:
            config["tool_config"] = NO_TOOL_CALLS
    start = time.perf_counter()
    try:
        response = await _generate(model, request_contents, types.GenerateContentConfig(**config))
    except errors.ClientError as e:
        if not cache_name or _is_billing_error(e):
            raise
        # 캐시가 만료·삭제된 경우: 캐시를 버리고 프롬프트를 직접 넣어 1회 다시 호출
        logger.warning("캐시 %s 사용 실패, 캐시 없이 다시 호출", cache_name)
        _drop_cache(cache_name)
        config.pop("cached_content")
        config["system_instruction"] = _join(system, context)
        config["tools"] = _to_gemini_tools(tools) if tools else None
        if no_tool_calls:
            config["tool_config"] = NO_TOOL_CALLS
        response = await _generate(model, contents, types.GenerateContentConfig(**config))
    if response.usage_metadata:
        _record_usage(model, response.usage_metadata, round((time.perf_counter() - start) * 1000))
    return response


def _join(system, context):
    return "\n\n".join(x for x in (system, context) if x) or None


def _with_context(contents, context):
    """명시적 캐시를 쓸 때: 첫 사용자 메시지 앞에 케이스 정보를 텍스트 파트로 붙인 사본."""
    if not context or not contents:
        return contents
    first = contents[0]
    return [types.Content(role=first.role, parts=[types.Part(text=context), *first.parts]), *contents[1:]]


def _to_contents(messages):
    """assistant 메시지의 trace(그 턴의 도구 호출·결과 Content)는 답변 텍스트 앞에 그대로 펼침."""
    role = {"user": "user", "assistant": "model"}
    contents = []
    for m in messages:
        contents += m.get("trace", ())
        contents.append(types.Content(role=role[m["role"]], parts=[
            *(types.Part.from_bytes(data=x["data"], mime_type=x["mime"]) for x in m.get("media", ())),
            types.Part(text=m["content"]),
        ]))
    return contents


async def generate(messages, system=None, tools=None, tool_executor=None, context=None, trace=None):
    """대화(messages)를 보내고 답변 텍스트를 반환. 실패 시 None.

    messages: [{"role": "user" | "assistant", "content": "...", "media": [{"mime", "data"}] (선택),
                "trace": [Content, ...] (선택, assistant)}, ...]
    system: 고정 시스템 프롬프트 (길어도 Gemini 컨텍스트 캐시로 재사용)
    context: 호출마다 달라지는 시스템 쪽 컨텍스트 (케이스 정보 등)
    tools / tool_executor: 도구 정의와 async (name, args) -> dict 실행 함수
    trace: 주면 이번 호출의 도구 호출·결과 Content를 차례로 담음 (대화 기록에 남겨 다음 턴에 같은 호출을 반복하지 않게)
    """
    contents = _to_contents(messages)
    if not (tools and tool_executor):
        tools = None
    try:
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            # 마지막 왕복은 도구 없이 지금까지의 결과로 답하게 함
            response = await _create(contents, system, tools, context, text_only=round_no == MAX_TOOL_ROUNDS)
            if not response.candidates:
                logger.warning("LLM 응답 없음(차단): %s", response.prompt_feedback)
                return None
            candidate = response.candidates[0]
            if candidate.finish_reason in (types.FinishReason.SAFETY, types.FinishReason.PROHIBITED_CONTENT):
                logger.warning("LLM이 요청을 거절함: %s", candidate.finish_reason)
                return None
            calls = response.function_calls
            if not calls:
                return response.text or None
            if round_no == MAX_TOOL_ROUNDS:
                # 도구를 막았는데도 호출이 섞여 오면, 같이 온 텍스트라도 답으로 씀
                text = "".join(p.text for p in candidate.content.parts if p.text and not p.thought)
                if text.strip():
                    return text
                break
            # 도구 호출: 모델 응답 전체(thought signature 포함)를 그대로 붙이고 결과를 한 메시지로 돌려줌
            # 호출과 결과는 다 실행된 뒤 한 쌍으로 붙임 (도중에 취소돼도 짝이 안 맞는 기록이 남지 않게)
            results = types.Content(role="user", parts=[
                types.Part(function_response=types.FunctionResponse(
                    id=c.id, name=c.name, response={"output": await tool_executor(c.name, c.args or {})},
                ))
                for c in calls
            ])
            contents += [candidate.content, results]
            if trace is not None:
                trace += [candidate.content, results]
        logger.warning("도구 호출 왕복 %d회 뒤에도 답이 없음", MAX_TOOL_ROUNDS)
        return None
    except (httpx.TimeoutException, asyncio.TimeoutError):
        logger.warning("LLM 호출 타임아웃")
    except errors.APIError as e:
        logger.warning("LLM API 오류 %s: %s", e.code, e.message)
    except Exception as e:  # 키 미설정, 네트워크 오류 등
        logger.warning("LLM 호출 실패: %s", e)
    return None
