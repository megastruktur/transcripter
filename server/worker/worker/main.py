"""Transcripter worker skeleton (Temporal connection lands in T3)."""

import asyncio
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("transcripter.worker")


async def main() -> None:
    log.info("worker skeleton: temporal loop starts in T3; sleeping")
    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
