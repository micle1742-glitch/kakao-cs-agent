"""가짜 액션 API. actions._send 자리에 끼워서 HTTP 대신 TC의 고정 응답을 돌려준다.

actions.request의 캐시·중복 접수 방지는 그대로 돌고, 실제로 '서버까지 간' 호출만 여기 기록된다.

TC의 api_fixtures 형식:
  API_03:                      # 도구 이름(operationId)
    "900000001": {...}         # 경로 변수 값별 응답 본문
    "*": {...}                 # 그 밖의 값 (없으면 GET은 404)
  API_10:
    "*": {_status: 409, error: "집하 후 변경 불가"}   # _status로 상태코드 지정
POST는 고정 응답이 없으면 접수번호를 만들어 준다. 이관 도구는 HO-YYYYMMDD-NNN(인입 일자 기준,
앞머리는 환경변수 MOCK_ESCALATION_PREFIX로 바꿈).
"""

import os
import re

import httpx


class MockActionAPI:
    def __init__(self, operations, fixtures=None, received_at=None, escalation_tool="API_35"):
        self.fixtures = fixtures or {}
        self.escalation_tool = escalation_tool
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", received_at or "")
        self.date = "".join(m.groups()) if m else "20370101"
        self.calls = []  # [{"tool", "method", "path", "params", "body", "status", "data"}]
        self._serial = {}
        self._routes = []
        for name, op in operations.items():
            pattern = re.sub(r"\\\{[^}]+\\\}", "([^/]+)", re.escape(op["path"]))
            self._routes.append((op["method"], re.compile(f"^{pattern}$"), name))

    def _route(self, method, path):
        for m, pattern, name in self._routes:
            match = pattern.match(path)
            if m == method and match:
                return name, (match.group(1) if match.groups() else None)
        return None, None

    def _respond(self, tool, method, key):
        table = self.fixtures.get(tool, {})
        if key is not None and str(key) in table:
            data = table[str(key)]
        elif "*" in table:
            data = table["*"]
        elif method == "GET":
            return 404, {"error": "not found"}
        else:
            n = self._serial[tool] = self._serial.get(tool, 0) + 1
            if tool == self.escalation_tool:
                prefix = os.getenv("MOCK_ESCALATION_PREFIX") or "HO"
                return 201, {"escalation_id": f"{prefix}-{self.date}-{n:03d}", "status": "접수"}
            return 201, {"receipt_no": f"RCV-{self.date}-{n:03d}", "status": "접수"}
        data = dict(data) if isinstance(data, dict) else data
        status = data.pop("_status", 200 if method == "GET" else 201) if isinstance(data, dict) else 200
        return status, data

    async def send(self, method, path, tc_id, params=None, body=None):
        tool, key = self._route(method.upper(), path)
        if tool is None:
            status, data = 404, {"error": f"unknown route {method} {path}"}
        else:
            status, data = self._respond(tool, method.upper(), key)
        self.calls.append({"tool": tool, "method": method.upper(), "path": path, "key": key, "params": params,
                           "body": body, "status": status, "data": data})
        return httpx.Response(status, json=data, request=httpx.Request(method, "http://mock" + path))
