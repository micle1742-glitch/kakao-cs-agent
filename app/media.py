"""카카오톡 사진·음성 내려받기.

두 가지 형태로 온다.
1) 카톡으로 직접 보낸 사진·음성: 별도 필드 없이 content 전체가 서명된 kakaocdn URL 하나
  사진: https://talk.kakaocdn.net/dna/.../i_xxxx.jpg?credential=...&expires=...&signature=...
  음성: https://talk.kakaocdn.net/dna/.../talka_aac.m4a?credential=...
2) 평가 TC의 초기 대화: 고객 문의 안에 "image: URL" / "audio: URL" 줄
  image: https://t1.kakaocdn.net/aitop100/aitop100/dist/xxxx.jpg
서버가 주는 Content-Type(image/jpg, audio/mp4a-latm)은 Gemini 표준이 아니라서 MIME은 확장자로 정한다.
"""

import html
import logging
import re
import time
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

MEDIA_URL = re.compile(r"^https://[\w.-]*kakaocdn\.net/\S+$")
MEDIA_LINE = re.compile(r"^\s*(image|audio)\s*:\s*(https://\S+)\s*$", re.IGNORECASE)
DEFAULT_MIME = {"image": "image/jpeg", "audio": "audio/mp4"}
MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".heic": "image/heic", ".heif": "image/heif",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".mp3": "audio/mp3", ".wav": "audio/wav", ".ogg": "audio/ogg",
}
MAX_BYTES = 15 * 1024 * 1024  # Gemini 요청 1건 인라인 데이터 한도(20MB) 안쪽
TIMEOUT = 10.0

logger = logging.getLogger("media")

_client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)


def _item(url, kind=None):
    mime = MIME_BY_EXT.get(PurePosixPath(urlsplit(url).path).suffix.lower()) or DEFAULT_MIME.get(kind)
    if mime is None:
        return None
    return {"url": url, "mime": mime, "label": "사진" if mime.startswith("image/") else "음성"}


def extract(content):
    """(사진·음성을 뺀 텍스트, [{"url", "mime", "label"}, ...])."""
    text = html.unescape((content or "").strip())
    if MEDIA_URL.match(text):
        item = _item(text)
        if item:
            return "", [item]
    items, rest = [], []
    for line in text.splitlines():
        m = MEDIA_LINE.match(line)
        item = m and _item(html.unescape(m.group(2)), m.group(1).lower())
        if item:
            items.append(item)
        else:
            rest.append(line)
    return ("\n".join(rest).strip(), items) if items else (text, [])


async def download(url):
    """내려받은 bytes. 실패하면 None."""
    start = time.perf_counter()
    try:
        resp = await _client.get(url)
    except httpx.HTTPError as e:
        logger.warning("내려받기 오류: %r", e)
        return None
    elapsed_ms = (time.perf_counter() - start) * 1000
    if not resp.is_success or not resp.content:
        logger.warning("내려받기 실패 status=%d %.0fms", resp.status_code, elapsed_ms)
        return None
    if len(resp.content) > MAX_BYTES:
        logger.warning("파일이 너무 큼 %dB", len(resp.content))
        return None
    logger.info("내려받음 %.0fKB %.0fms (%s)", len(resp.content) / 1024, elapsed_ms, resp.headers.get("content-type"))
    return resp.content


async def close():
    await _client.aclose()
