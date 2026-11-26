"""상태 확인(/health)과 감시: 클라우드에서 봇이 살아 있는지 밖에서 볼 수 있게 한다.

이 봇은 웹 서버가 아니라 게이트웨이에 웹소켓으로 붙는 워커라서, 그대로는 플랫폼이 "살아 있나"를 알 수 없다.
그래서 작은 HTTP 서버를 따로 띄운다 (표준 라이브러리 asyncio만 사용).
  GET /health → 200 {"status":"ok", ...} / 게이트웨이가 오래 끊겨 있으면 503
감시(watchdog): 게이트웨이가 WATCHDOG_SECONDS 넘게 끊겨 있으면 프로세스를 끝낸다 → 플랫폼 재시작 정책이 다시 띄운다.
비밀값(토큰·키)은 응답에 넣지 않는다.
"""

import asyncio
import json
import logging
import os
import time

from app import prompt

logger = logging.getLogger("health")
STARTED = time.time()
STATE = {"connected": False, "since": time.time(), "last_event": None, "last_heartbeat": None, "messages": 0}
GRACE_SECONDS = float(os.getenv("HEALTH_GRACE_SECONDS") or "120")      # 이만큼은 끊겨도 재연결 중으로 봄
WATCHDOG_SECONDS = float(os.getenv("WATCHDOG_SECONDS") or "600")       # 이보다 오래 끊기면 프로세스 종료


def set_connected(flag):
    if STATE["connected"] != flag:
        STATE["connected"], STATE["since"] = flag, time.time()


def mark(kind):
    STATE[kind] = time.time()
    if kind == "last_event":
        STATE["messages"] += 1


def snapshot():
    now = time.time()
    down_for = 0 if STATE["connected"] else now - STATE["since"]
    healthy = STATE["connected"] or down_for < GRACE_SECONDS
    try:
        kb = json.loads((prompt.KB_DIR / "version.json").read_text(encoding="utf-8"))
        kb = {k: kb.get(k) for k in ("builtAt", "cards", "conflicts")}
    except (OSError, ValueError):
        kb = None

    def age(t):
        return None if t is None else round(now - t)
    return healthy, {
        "status": "ok" if healthy else "gateway_down", "uptime": round(now - STARTED),
        "gateway": {"connected": STATE["connected"], "downFor": round(down_for),
                    "lastEventAgo": age(STATE["last_event"]), "lastHeartbeatAgo": age(STATE["last_heartbeat"])},
        "messages": STATE["messages"], "kb": kb,
    }


async def _handle(reader, writer):
    try:
        line = (await asyncio.wait_for(reader.readline(), 5)).decode("latin-1")
        path = line.split(" ")[1] if line.count(" ") >= 2 else "/"
        if path.split("?")[0] == "/health":
            healthy, body = snapshot()
            status = "200 OK" if healthy else "503 Service Unavailable"
        else:
            status, body = "404 Not Found", {"error": "not found"}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json; charset=utf-8\r\n"
                     f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
        await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, IndexError):
        pass
    finally:
        writer.close()


async def serve(port):
    server = await asyncio.start_server(_handle, "0.0.0.0", port)
    logger.info("상태 확인 서버 :%d/health", port)
    return server


async def watchdog(exit_fn=os._exit, interval=30):
    """게이트웨이가 너무 오래 끊겨 있으면 프로세스를 끝내 플랫폼이 새로 띄우게 한다."""
    while True:
        await asyncio.sleep(interval)
        if not STATE["connected"] and time.time() - STATE["since"] > WATCHDOG_SECONDS:
            logger.error("게이트웨이가 %.0f초 넘게 끊겨 있어 프로세스를 끝냄 (재시작 정책이 다시 띄움)", WATCHDOG_SECONDS)
            exit_fn(1)
