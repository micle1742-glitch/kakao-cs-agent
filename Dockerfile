# 카카오톡 봇 CS 에이전트 — 항상 켜진 워커 (포트 없음, 게이트웨이에 웹소켓으로 접속)
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

# 의존성만 먼저 설치 (코드가 바뀌어도 이 층은 캐시됨)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
# knowledge/ 는 대회 자료라 저장소에 없습니다. 실행할 때 마운트하세요.
COPY main.py reset.py ./

# 대화 기록 DB(state.db)와 usage.log·api_usage.log·events.log는 /data에 쌓인다 → 플랫폼 볼륨을 /data에 연결
VOLUME ["/data"]

CMD ["/app/.venv/bin/python", "main.py"]
