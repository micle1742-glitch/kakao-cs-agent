"""카카오 봇 콜백 답장."""

import logging

import httpx

CALLBACK_URL = "https://kapi.kakao.com/v1/bot/callback"
MAX_TEXT_LENGTH = 500
MAX_ATTEMPTS = 2  # 실패 시 1회 재시도

logger = logging.getLogger("kakao")

_client = httpx.AsyncClient(timeout=5.0)


def build_body(text):
    """simpleText 1개만 담은 답장 본문. 500자를 넘으면 자름."""
    return {
        "skillResponse": {
            "version": "2.0",
            "template": {
                "outputs": [{"simpleText": {"text": text[:MAX_TEXT_LENGTH]}}],
            },
        },
    }


async def send_reply(bot_token, callback_token, text):
    """콜백으로 답장. 성공하면 True."""
    headers = {
        "Authorization": f"KakaoAK {bot_token}",
        "X-Bot-Callback-Token": callback_token,
    }
    body = build_body(text)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = await _client.post(CALLBACK_URL, json=body, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("콜백 전송 오류 (시도 %d/%d): %r", attempt, MAX_ATTEMPTS, e)
            continue
        result = _parse_counts(resp)
        if resp.is_success:
            logger.info("콜백 성공 (시도 %d/%d) status=%d %s", attempt, MAX_ATTEMPTS, resp.status_code, result)
            return True
        logger.warning("콜백 실패 (시도 %d/%d) status=%d %s body=%s",
                       attempt, MAX_ATTEMPTS, resp.status_code, result, resp.text[:300])
    return False


def _parse_counts(resp):
    try:
        data = resp.json()
    except ValueError:
        return "sendCount=? errorCount=?"
    if not isinstance(data, dict):
        return "sendCount=? errorCount=?"
    return f"sendCount={data.get('sendCount', '?')} errorCount={data.get('errorCount', '?')}"


async def close():
    await _client.aclose()
