"""Compare anonymous Telegram HTTPS connectivity by address family."""

import asyncio
import socket
import time

import aiohttp


async def probe(family: int) -> tuple[int, float]:
    success = 0
    elapsed = 0.0
    connector = aiohttp.TCPConnector(family=family, force_close=True)
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for _ in range(5):
            started = time.monotonic()
            try:
                async with session.get("https://api.telegram.org", allow_redirects=False) as response:
                    if response.status in (200, 302):
                        success += 1
            except (aiohttp.ClientError, TimeoutError):
                pass
            elapsed += time.monotonic() - started
    return success, round(elapsed, 1)


async def main() -> None:
    for label, family in (("default", socket.AF_UNSPEC), ("IPv4", socket.AF_INET)):
        success, seconds = await probe(family)
        print(f"Telegram HTTPS {label}: {success}/5 successful, {seconds}s total")


if __name__ == "__main__":
    asyncio.run(main())
