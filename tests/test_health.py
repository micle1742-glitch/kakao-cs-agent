"""상태 확인 서버와 감시 시험 (네트워크는 이 컴퓨터 안에서만)."""

import asyncio
import json
import time

from app import health


async def _get(port, path):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.decode("utf-8").partition("\r\n\r\n")
    return head.split(" ")[1], body


def test_health_ok_and_down(monkeypatch):
    async def run():
        server = await health.serve(0)
        port = server.sockets[0].getsockname()[1]
        health.set_connected(True)
        health.mark("last_event")
        code, body = await _get(port, "/health")
        assert code == "200" and json.loads(body)["gateway"]["connected"] is True
        health.set_connected(False)
        health.STATE["since"] = time.time() - health.GRACE_SECONDS - 5    # 오래 끊김
        code, body = await _get(port, "/health")
        assert code == "503" and json.loads(body)["status"] == "gateway_down"
        code, _ = await _get(port, "/other")
        assert code == "404"
        server.close()
    asyncio.run(run())


def test_watchdog_exits_when_down_too_long(monkeypatch):
    exited = []
    health.set_connected(False)
    health.STATE["since"] = time.time() - health.WATCHDOG_SECONDS - 5

    async def run():
        task = asyncio.create_task(health.watchdog(exit_fn=exited.append, interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
    asyncio.run(run())
    assert exited and exited[0] == 1
    health.set_connected(True)
