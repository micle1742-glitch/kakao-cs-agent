"""카카오 봇 게이트웨이 연결, heartbeat, 재연결."""

import asyncio
import json
import logging
import random
import time

import websockets

from app import store
from app.handler import handle_message

GW_URL = "wss://bot-gateway.kakao.com/gateway"
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_HELLO = 10
MAX_BACKOFF_SECONDS = 30
LOG_FIRST_EVENTS = 5  # MESSAGE_CREATE 원본 JSON을 처음 몇 개 로그로 남김
# 모든 MESSAGE_CREATE 원본을 한 줄씩(JSON) 남기는 파일 (사진·음성 등 필드 구조 확인용, callbackToken은 가림)
EVENTS_LOG = store.DATA_DIR / "events.log"

logger = logging.getLogger("gateway")


def _mask(d):
    """로그용: callbackToken 값은 가림."""
    masked = dict(d)
    if masked.get("callbackToken"):
        masked["callbackToken"] = "***"
    return masked


class Gateway:
    def __init__(self, bot_token):
        self.bot_token = bot_token
        self.logged_events = 0
        self.tasks = set()  # 처리 중인 메시지 task (GC 방지)

    async def run_forever(self):
        """끊기면 지수 백오프(최대 30초)로 재연결. RESUME은 튜토리얼에 없어서 매번 IDENTIFY."""
        backoff = 1
        while True:
            connected = False
            try:
                async with websockets.connect(GW_URL) as ws:
                    await self._identify(ws)
                    connected = True
                    backoff = 1
                    await self._listen(ws)
                logger.warning("게이트웨이 연결 종료")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("게이트웨이 오류: %r", e)
            delay = min(backoff + random.uniform(0, backoff / 2), MAX_BACKOFF_SECONDS)
            logger.info("%.1f초 후 재연결", delay)
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    async def _identify(self, ws):
        hello = json.loads(await ws.recv())
        if hello.get("op") != OP_HELLO:
            raise RuntimeError(f"HELLO 대신 op={hello.get('op')} 수신")
        self.heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000
        await ws.send(json.dumps({"op": OP_IDENTIFY, "d": {"token": self.bot_token}}))
        ready = json.loads(await ws.recv())
        logger.info("게이트웨이 연결 성공 (heartbeat %.1fs, READY op=%s t=%s)",
                    self.heartbeat_interval, ready.get("op"), ready.get("t"))

    async def _listen(self, ws):
        heartbeat = asyncio.create_task(self._heartbeat(ws))
        try:
            async for raw in ws:
                event = json.loads(raw)
                if event.get("t") == "MESSAGE_CREATE":
                    self._dispatch(event)
        finally:
            heartbeat.cancel()

    def _dispatch(self, event):
        received_at = time.perf_counter()
        d = event.get("d") or {}
        raw = json.dumps({**event, "d": _mask(d)}, ensure_ascii=False)
        if self.logged_events < LOG_FIRST_EVENTS:
            self.logged_events += 1
            logger.info("MESSAGE_CREATE 원본 (%d/%d): %s", self.logged_events, LOG_FIRST_EVENTS, raw)
        try:
            with EVENTS_LOG.open("a", encoding="utf-8") as f:
                f.write(raw + "\n")
        except OSError as e:
            logger.warning("events.log 기록 실패: %r", e)
        # 메시지마다 별도 task로 처리 (느린 메시지가 다른 메시지를 막지 않게)
        task = asyncio.create_task(handle_message(self.bot_token, d, received_at))
        self.tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            logger.error("메시지 처리 중 오류", exc_info=task.exception())

    async def _heartbeat(self, ws):
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                await ws.send(json.dumps({"op": OP_HEARTBEAT}))
        except websockets.ConnectionClosed:
            pass
