import uvicorn

from config import settings
from utils.log_setup import LogSetup


def main() -> None:
    LogSetup.configure()
    uvicorn.run(
        "app.main:app",
        host=settings.server_host,
        port=settings.server_port,
        reload=settings.server_reload,
        log_config=None,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
