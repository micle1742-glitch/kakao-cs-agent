"""액션 API 연동. OpenAPI 문서의 모든 API를 LLM 도구(function calling)로 등록하고 실행한다.

- OpenAPI 문서: knowledge/openapi.(yaml|yml|json)이 있으면 그것을, 없으면 서버의 /api/openapi.json을 읽는다.
- 헤더: Authorization: Bearer {ACTION_TOKEN}, X-Aitop100-Test-Case-Id: {TC ID}
- TC ID(case metadata의 id, handler가 넘김)가 없으면 API를 부르지 않고 로그만 남긴다.
- 429는 Retry-After만큼, 500/503은 1초 기다린 뒤 1회 재시도.
- 같은 TC 안의 같은 GET 요청은 캐시해서 다시 부르지 않는다 (평가 누적 한도 3,000회).
- 모든 호출은 메서드·경로·상태코드·걸린 시간과 누적 호출 수를 로그와 api_usage.log에 남긴다.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml

from app import calendar, coupons, refunds, store

BASE_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
API_USAGE_LOG = store.DATA_DIR / "api_usage.log"
BASE_URL = os.getenv("ACTION_BASE_URL", "https://gyodong-mall-action.aitop100.org")
SPEC_PATH = "/api/openapi.json"
RESET_PATH = "/api/action-data/reset"  # 연습용 초기화. LLM 도구로는 등록하지 않음 (reset.py로만)
TIMEOUT = 10.0
RETRY_STATUSES = (500, 503)

POST_NOTE = " 정책 확인 후에만 호출한다. 접수는 정책 검사를 하지 않으므로, knowledge 규정으로 처리 가능 여부를 먼저 확인한 뒤 호출한다."
NO_TC_ID = {"ok": False, "error": "테스트 케이스 ID가 없어 API를 호출하지 않았습니다. 고객에게는 확인 후 안내하겠다고 답하세요."}

logger = logging.getLogger("actions")

_client = None
_get_cache = {}  # (tc_id, url, params) -> 결과. 같은 TC 안의 같은 GET은 다시 부르지 않음
_operations = {}  # 도구 이름 -> {"method", "path", "path_params", "query_params", "body_keys", "body_whole", "types"}
TOOLS = []


def _headers(tc_id):
    headers = {"Authorization": f"Bearer {os.getenv('ACTION_TOKEN', '')}"}
    if tc_id:
        headers["X-Aitop100-Test-Case-Id"] = tc_id
    return headers


def _get_client():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT)
    return _client


async def close():
    if _client is not None:
        await _client.aclose()


def _load_total():
    """api_usage.log 마지막 줄에서 누적 호출 수를 복원 (재시작 후에도 이어서 누적)."""
    try:
        last = API_USAGE_LOG.read_text(encoding="utf-8").strip().splitlines()[-1]
        return json.loads(last)["total_calls"]
    except (FileNotFoundError, IndexError, ValueError, KeyError):
        return 0


def _record_call(method, path, status, elapsed_ms, tc_id):
    total = _load_total() + 1
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "method": method,
        "path": path,
        "status": status,
        "elapsed_ms": elapsed_ms,
        "tc_id": tc_id,
        "total_calls": total,
    }
    with API_USAGE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.info("API %s %s -> %s %dms (tc=%s, 누적 %d회)", method, path, status, elapsed_ms, tc_id, total)


async def _send(method, path, tc_id, params=None, body=None):
    start = time.perf_counter()
    try:
        response = await _get_client().request(method, path, params=params, json=body, headers=_headers(tc_id))
    except httpx.HTTPError as e:
        _record_call(method, path, f"error {type(e).__name__}", round((time.perf_counter() - start) * 1000), tc_id)
        raise
    _record_call(method, path, response.status_code, round((time.perf_counter() - start) * 1000), tc_id)
    return response


def _retry_after(response):
    try:
        return max(float(response.headers.get("Retry-After", "1")), 0)
    except ValueError:  # HTTP 날짜 형식 등은 1초로
        return 1.0


async def request(method, path, tc_id, params=None, body=None):
    """API 1회 호출 (429·500·503은 1회 재시도). 결과는 {"ok", "status", "data"}."""
    method = method.upper()
    cache_key = (tc_id, path, json.dumps(params, sort_keys=True, ensure_ascii=False)) if method == "GET" else None
    if cache_key in _get_cache:
        logger.info("API %s %s 캐시 사용 (tc=%s)", method, path, tc_id)
        return _get_cache[cache_key]
    # 같은 TC에서 이미 성공한 똑같은 접수는 다시 보내지 않음 (중복 접수 방지)
    submit_key = None if method == "GET" else (tc_id, method, path, json.dumps(body, sort_keys=True, ensure_ascii=False))
    previous = store.load_submission(json.dumps(submit_key, ensure_ascii=False)) if submit_key else None
    if previous:
        logger.warning("API %s %s 같은 접수가 이미 성공해 다시 보내지 않음 (tc=%s)", method, path, tc_id)
        return {**previous, "duplicate": True,
                "note": "같은 요청이 이 상담에서 이미 접수되어 다시 보내지 않았습니다. 앞선 접수 결과를 안내하세요."}

    response = await _send(method, path, tc_id, params, body)
    if response.status_code == 429:
        wait = _retry_after(response)
        logger.warning("API 429, %.1f초 후 재시도", wait)
        await asyncio.sleep(wait)
        response = await _send(method, path, tc_id, params, body)
    elif response.status_code in RETRY_STATUSES:
        logger.warning("API %d, 1초 후 재시도", response.status_code)
        await asyncio.sleep(1)
        response = await _send(method, path, tc_id, params, body)

    try:
        data = response.json()
    except ValueError:
        data = response.text
    result = {"ok": response.is_success, "status": response.status_code, "data": data}
    # 일시 오류(429·5xx)는 캐시하지 않음
    if cache_key and response.status_code != 429 and response.status_code < 500:
        _get_cache[cache_key] = result
    if submit_key and response.is_success:
        store.save_submission(json.dumps(submit_key, ensure_ascii=False), result)
    return result


# --- OpenAPI 문서 → 도구 정의 ---

SCHEMA_KEYS = {"type", "description", "enum", "format", "items", "properties", "required",
               "minimum", "maximum", "minLength", "maxLength", "pattern", "default", "anyOf"}


def _resolve(spec, node):
    """$ref를 풀고, Gemini 함수 선언에 쓸 수 있는 JSON Schema 키만 남긴다."""
    if isinstance(node, list):
        return [_resolve(spec, n) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = _resolve_ref(spec, {"$ref": node["$ref"]})
        return _resolve(spec, {**target, **{k: v for k, v in node.items() if k != "$ref"}})
    if "allOf" in node:  # allOf는 한 객체로 합침
        merged = {"type": "object", "properties": {}, "required": []}
        for sub in _resolve(spec, node["allOf"]):
            merged["properties"].update(sub.get("properties", {}))
            merged["required"] += sub.get("required", [])
        return merged
    if "oneOf" in node:
        node = {**node, "anyOf": node["oneOf"]}
    out = {}
    for k, v in node.items():
        if k not in SCHEMA_KEYS:
            continue
        if k == "properties":
            out[k] = {name: _resolve(spec, sub) for name, sub in v.items()}
        elif k == "type" and isinstance(v, list):  # OpenAPI 3.1 ["string", "null"] → "string"
            out[k] = next((t for t in v if t != "null"), "string")
        elif k in ("items", "anyOf"):
            out[k] = _resolve(spec, v)
        else:
            out[k] = v
    return out


def _resolve_ref(spec, node):
    while isinstance(node, dict) and "$ref" in node:
        target = spec
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        node = target
    return node


def _tool_name(op, method, path):
    name = op.get("operationId") or f"{method}_{path}"
    return re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_")[:64]


def _build_tool(spec, method, path, op, shared_params):
    """OpenAPI operation 하나를 도구 정의와 실행 정보로 바꾼다."""
    properties, required = {}, []
    path_params, query_params = [], []
    for p in shared_params + op.get("parameters", []):
        p = _resolve_ref(spec, p)
        if p.get("in") not in ("path", "query"):
            continue  # 헤더는 직접 넣음
        schema = _resolve(spec, p.get("schema", {"type": "string"}))
        if p.get("description"):
            schema["description"] = p["description"]
        properties[p["name"]] = schema
        if p.get("required") or p["in"] == "path":
            required.append(p["name"])
        (path_params if p["in"] == "path" else query_params).append(p["name"])

    # body_keys: None이면 본문 없음, 목록이면 그 필드만 담은 JSON 객체(필드가 없어도 {})를 보냄
    body_keys, body_whole = None, False
    body = _resolve_ref(spec, op.get("requestBody", {}))
    body_schema = body.get("content", {}).get("application/json", {}).get("schema")
    if body_schema:
        body_schema = _resolve(spec, body_schema)
        body_props = body_schema.get("properties", {})
        if body_schema.get("type") == "object" and not set(body_props) & set(properties):
            # 본문 필드를 도구 인자로 펼침 (additionalProperties: false라 정의된 필드만 보냄)
            properties.update(body_props)
            required += body_schema.get("required", [])
            body_keys = list(body_props)
        else:
            properties["body"] = body_schema
            if body.get("required"):
                required.append("body")
            body_whole = True

    url_path = path.partition("?")[0]  # 문서의 경로 키에 붙은 "?item=" 같은 쿼리 표기는 떼고 params로 보냄
    tags = "·".join(op.get("tags", []))
    summary = " ".join(x for x in (op.get("summary"), op.get("description")) if x)
    description = f"[{tags}] {summary} ({method.upper()} {path})" if tags else f"{summary} ({method.upper()} {path})"
    if method != "get":
        description += POST_NOTE
    name = _tool_name(op, method, path)
    _operations[name] = {
        "method": method.upper(), "path": url_path, "path_params": path_params,
        "query_params": query_params, "body_keys": body_keys, "body_whole": body_whole,
        "types": {k: v.get("type") for k, v in properties.items()},
    }
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = sorted(set(required))
    return {"name": name, "description": description, "input_schema": schema}


def _load_spec():
    for name in ("openapi.yaml", "openapi.yml", "openapi.json"):
        path = KNOWLEDGE_DIR / name
        if path.exists():
            logger.info("OpenAPI 문서 %s 사용", path.name)
            return yaml.safe_load(path.read_text(encoding="utf-8"))  # JSON도 YAML로 읽힘
    logger.info("knowledge/에 OpenAPI 문서가 없어 서버 %s%s 에서 읽음", BASE_URL, SPEC_PATH)
    start = time.perf_counter()
    response = httpx.get(BASE_URL + SPEC_PATH, headers=_headers(None), timeout=TIMEOUT)
    _record_call("GET", SPEC_PATH, response.status_code, round((time.perf_counter() - start) * 1000), None)
    response.raise_for_status()
    return response.json()


def load_tools():
    """OpenAPI 문서를 읽어 TOOLS를 채우고 반환. 실패하면 빈 목록."""
    try:
        spec = _load_spec()
    except Exception as e:
        logger.error("OpenAPI 문서를 읽지 못해 도구 없이 동작: %s", e)
        return []
    TOOLS.clear()
    _operations.clear()
    for path, item in spec.get("paths", {}).items():
        if path == RESET_PATH:
            continue
        shared = item.get("parameters", [])
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                TOOLS.append(_build_tool(spec, method, path, op, shared))
    TOOLS.extend(t for t, _ in LOCAL_TOOLS.values())
    logger.info("도구 %d개 등록: %s", len(TOOLS), ", ".join(t["name"] for t in TOOLS))
    return TOOLS


# --- 서버를 부르지 않는 로컬 계산 도구 (TC ID 불필요, API 호출 수에 안 잡힘) ---

def _subscription_cutoff(args):
    pay_date = calendar.parse_date(args["pay_date"])
    cutoff = calendar.subscription_cutoff(pay_date)
    return {"ok": True, "pay_date": calendar.fmt(pay_date), "cutoff": f"{calendar.fmt(cutoff)} 24:00",
            "note": "컷오프 이전 접수는 이번 회차에 반영, 이후 접수는 다음 회차부터 반영"}


def _next_shipping_day(args):
    base = calendar.parse_date(args["base"])
    ship = calendar.next_shipping_day(base)
    arrive_from, arrive_to = ship + timedelta(days=1), ship + timedelta(days=2)
    return {"ok": True, "base": calendar.fmt(base), "next_shipping_day": calendar.fmt(ship),
            "expected_arrival": f"{calendar.fmt(arrive_from)} ~ {calendar.fmt(arrive_to)} (통상 익일~모레, 도착일은 보장하지 않음)",
            "note": "산지 출고는 월·수·금 영업일. 당일배송 불가"}


def _pickup_deadline(args):
    pickup = calendar.parse_date(args["pickup_date"])
    if not calendar.is_business_day(pickup):
        return {"ok": False, "pickup_date": calendar.fmt(pickup),
                "error": "픽업은 영업일 오전에만 운영합니다. 수령 희망일이 휴무일(토·일·공휴일)입니다."}
    return {"ok": True, "pickup_date": calendar.fmt(pickup),
            "booking_deadline": f"{calendar.fmt(calendar.pickup_deadline(pickup))} 24:00"}


def _care_coupons(args):
    diff = int(float(args["diff"]))
    codes = coupons.care_coupons(diff)
    total = sum(int(c[4:]) for c in codes)
    return {"ok": True, "diff": diff, "coupons": codes, "total": total,
            "note": "가격 인하 차액 보상 — 차액 이상 최소 금액. 차액을 넘는 부분은 정산하지 않음. 각 쿠폰은 발급일 + 7일 유효(예시 값, 실제 규정은 대회 자료라 비공개)"}


def _partial_refund(args):
    result = refunds.partial_refund(args["paid"], args["defects"], args["count"])
    return {"ok": True, **result,
            "note": "근거 제시 순서: 입수 → 하자 수 → 비율 → 실결제액 → 환불액. 부분환불은 배송비 환급 없음. "
                    "재배송을 고르면 환불하지 않음. 재량 한도를 넘으면 운영 승인"}


LOCAL_TOOLS = {
    "calc_partial_refund": ({
        "name": "calc_partial_refund",
        "description": "[환불] 박스·세트 상품에서 일부 개체만 하자일 때 하자 비율(R)·구간·환불액(또는 선택지)을 계산한다. "
                       "서버 호출이 아닌 계산이다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "paid": {"type": "integer", "description": "해당 상품의 실결제액(원) — 주문 조회 값"},
                "defects": {"type": "integer", "description": "하자 개체 수"},
                "count": {"type": "integer", "description": "입수(한 박스 개체 수) — SKU 카탈로그 값, 중량판매면 고객이 센 실물 개수"},
            },
            "required": ["paid", "defects", "count"],
        },
    }, _partial_refund),
    "calc_care_coupons": ({
        "name": "calc_care_coupons",
        "description": "[쿠폰] 가격 인하 차액(원)에 맞는 보상 쿠폰 조합(예시 값: DEMO2000/4000/8000)을 계산한다. "
                       "쿠폰 발급(API-33)은 이 결과의 코드로 한다. 서버 호출이 아닌 계산이다.",
        "input_schema": {
            "type": "object",
            "properties": {"diff": {"type": "integer", "description": "가격 인하 차액(원) = 구매 당시 실결제가 - 현재 정상가"}},
            "required": ["diff"],
        },
    }, _care_coupons),
    "calc_pickup_deadline": ({
        "name": "calc_pickup_deadline",
        "description": "[달력] 산지 픽업 예약 마감(수령 희망일의 1영업일 전 24:00)을 계산한다. 수령 희망일이 휴무일이면 알려 준다. 서버 호출이 아닌 계산이다.",
        "input_schema": {
            "type": "object",
            "properties": {"pickup_date": {"type": "string", "description": "수령 희망일 YYYY-MM-DD"}},
            "required": ["pickup_date"],
        },
    }, _pickup_deadline),
    "calc_next_shipping_day": ({
        "name": "calc_next_shipping_day",
        "description": "[달력] 기준 시각(보통 케이스 인입 일시 또는 주문 시각) 다음의 산지 출고일(월·수·금 영업일, 공휴일 회차 건너뜀)과 "
                       "예상 도착 범위를 계산한다. 언제 받는지·급하다·특정 날짜까지 필요하다는 문의에 쓴다. 서버 호출이 아닌 계산이다.",
        "input_schema": {
            "type": "object",
            "properties": {"base": {"type": "string", "description": "기준 날짜 YYYY-MM-DD (인입 일시의 날짜)"}},
            "required": ["base"],
        },
    }, _next_shipping_day),
    "calc_subscription_cutoff": ({
        "name": "calc_subscription_cutoff",
        "description": "[달력] 정기배송 회차 결제일의 컷오프(결제일 3영업일 전 24:00, 공휴일 반영)를 계산한다. "
                       "스킵·해지·변경 요청이 이번 회차에 반영되는지 판단할 때 쓴다. 서버 호출이 아닌 계산이다.",
        "input_schema": {
            "type": "object",
            "properties": {"pay_date": {"type": "string", "description": "회차 결제일 YYYY-MM-DD"}},
            "required": ["pay_date"],
        },
    }, _subscription_cutoff),
}


def _coerce(value, type_):
    """LLM이 준 값을 문서의 타입에 맞춤 (예: 30000.0 → 30000, 주문번호 100400001 → "100400001")."""
    if type_ == "integer" and isinstance(value, float) and value.is_integer():
        return int(value)
    if type_ == "string" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
    return value


async def execute(name, args, tc_id):
    """도구 호출 실행. 결과는 function_response에 넣을 dict."""
    if name in LOCAL_TOOLS:
        try:
            result = LOCAL_TOOLS[name][1](args)
        except (KeyError, ValueError) as e:
            result = {"ok": False, "error": f"계산할 수 없는 입력: {e}"}
        logger.info("로컬 도구 %s %s -> %s", name, args, result)
        return result
    op = _operations.get(name)
    if op is None:
        return {"ok": False, "error": f"알 수 없는 도구: {name}"}
    if not tc_id:
        logger.warning("TC ID 없음, API 호출 생략: %s %s args=%s", op["method"], op["path"], args)
        return NO_TC_ID
    args = {k: _coerce(v, op["types"].get(k)) for k, v in args.items()}
    path = op["path"]
    for p in op["path_params"]:
        path = path.replace("{" + p + "}", quote(str(args.get(p, "")), safe=""))
    params = {p: args[p] for p in op["query_params"] if args.get(p) is not None} or None
    if op["body_whole"]:
        body = args.get("body")
    elif op["body_keys"] is not None:
        body = {k: args[k] for k in op["body_keys"] if k in args}
    else:
        body = None
    try:
        return await request(op["method"], path, tc_id, params, body)
    except httpx.HTTPError as e:
        logger.warning("도구 %s 호출 오류: %r", name, e)
        return {"ok": False, "error": "API 호출 중 오류"}
