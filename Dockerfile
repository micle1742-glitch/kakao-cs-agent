# 카카오톡 봇 CS 에이전트 — 항상 켜진 워커 (게이트웨이에 웹소켓으로 접속)
# 8080은 바깥 요청을 받는 곳이 아니라 상태 확인(/health)용이다.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    KNOWLEDGE_DIR=/data/knowledge \
    KB_DIR=/data/kb/current \
    HEALTH_PORT=8080

# 의존성만 먼저 설치 (코드가 바뀌어도 이 층은 캐시됨)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 봇이 실행 중에 읽는 코드와 설정: app(봇), kb(카드 글 만들기·근거 대조), rules.yaml(우선순위 헌법 기본값)
COPY app ./app
COPY kb ./kb
COPY rules.yaml main.py reset.py ./

# 대회 자료(knowledge/, kb 카드, 대회용 rules.yaml)는 이미지에 넣지 않는다 → 볼륨(/data)에 올린다 (DEPLOY.md)
# 대화 기록 DB(state.db)와 로그도 /data에 쌓인다
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD /app/.venv/bin/python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"

CMD ["/app/.venv/bin/python", "main.py"]
