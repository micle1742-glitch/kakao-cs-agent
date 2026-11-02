import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# app 모듈이 import 시점에 남기는 로그(도구 등록 등)도 보이도록 import 전에 설정
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx 요청 로그는 끔 (헤더/토큰이 찍히지 않게, 콜백·API 결과는 kakao·actions 로그로 남김)
logging.getLogger("httpx").setLevel(logging.WARNING)

from app import actions, kakao, media  # noqa: E402
from app.gateway import Gateway  # noqa: E402


async def main():
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        sys.exit("BOT_TOKEN이 .env에 없습니다. README의 .env 설정을 확인하세요.")
    try:
        await Gateway(bot_token).run_forever()
    finally:
        await kakao.close()
        await actions.close()
        await media.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
