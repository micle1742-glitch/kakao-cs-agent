"""연습용: 액션 API 데이터 초기화 (POST /api/action-data/reset).

사용법: uv run reset.py [TC ID]
"""

import asyncio
import json
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

from app import actions, store  # noqa: E402


async def main():
    tc_id = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        result = await actions.request("POST", actions.RESET_PATH, tc_id)
    finally:
        await actions.close()
    if result["ok"]:
        # 서버 데이터를 처음 상태로 돌렸으니, 같은 접수를 막던 중복 방지 기록도 지운다
        store.clear_submissions()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
