import asyncio
import signal

from utils.log_setup import LogSetup

from .service import run_publisher


async def _main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await run_publisher(stop)


def main() -> None:
    LogSetup.configure()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
