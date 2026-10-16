import sys

from telegram_gateway.login import login
from telegram_gateway.runtime import run_gateway

import asyncio


if __name__ == "__main__":
    asyncio.run(login() if "--login" in sys.argv[1:] else run_gateway())
