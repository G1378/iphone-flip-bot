from __future__ import annotations

import asyncio
import logging
import signal

from app.bot.client import build_bot
from app.config import settings
from app.logging_conf import setup_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()

    if not settings.discord_bot_token or settings.discord_bot_token == "dev-placeholder":
        logger.error(
            "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill in your bot token "
            "(see README 'Discord bot setup')."
        )
        return

    bot = build_bot()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal(sig_name: str) -> None:
        logger.info("Received %s - shutting down gracefully...", sig_name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig.name)
        except NotImplementedError:
            # add_signal_handler isn't available on Windows - not a
            # supported deployment target for this app (Raspberry Pi
            # only), but avoid a hard crash if someone runs it there.
            pass

    async with bot:
        bot_task = asyncio.create_task(bot.start(settings.discord_bot_token))
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait({bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

        if stop_task in done:
            await bot.close()
        for task in pending:
            task.cancel()

        # surface any startup/connection error instead of swallowing it
        if bot_task in done and bot_task.exception():
            raise bot_task.exception()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
