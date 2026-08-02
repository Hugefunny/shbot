"""
图片词条机器人 - 入口文件
使用方式:
  epsdk run main.py
  python main.py
"""

import asyncio
from ErisPulse import sdk


async def main():
    print("正在启动图片词条机器人...")
    sdk.logger.info("图片词条机器人启动中")
    await sdk.run(keep_running=True)


if __name__ == "__main__":
    asyncio.run(main())