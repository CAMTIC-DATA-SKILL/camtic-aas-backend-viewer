"""레포 전체 공용 로그 설정.

모든 모듈은 ``logger = LogSetup.get_logger(__name__)`` 로 로거를 얻어 사용한다.
uvicorn, BaSyx SDK 등 외부 라이브러리 로그도 루트 로거로 전파되어 같은 형식/출력으로 기록된다.
"""

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

from config import settings

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class LogSetup:
    _configured = False
    _lock = threading.Lock()

    @classmethod
    def configure(cls) -> None:
        with cls._lock:
            if cls._configured:
                return

            formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
            handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
            if settings.log_dir is not None:
                settings.log_dir.mkdir(parents=True, exist_ok=True)
                handlers.append(
                    RotatingFileHandler(
                        settings.log_dir / settings.log_file_name,
                        maxBytes=settings.log_max_bytes,
                        backupCount=settings.log_backup_count,
                        encoding="utf-8",
                    )
                )

            root = logging.getLogger()
            for handler in list(root.handlers):
                root.removeHandler(handler)
            for handler in handlers:
                handler.setFormatter(formatter)
                root.addHandler(handler)
            root.setLevel(settings.log_level)

            for name in _UVICORN_LOGGERS:
                uvicorn_logger = logging.getLogger(name)
                uvicorn_logger.handlers.clear()
                uvicorn_logger.propagate = True
            # SERVER_RELOAD 사용 시 파일 변경마다 INFO 로그를 남기므로 경고 이상만 기록한다.
            logging.getLogger("watchfiles").setLevel(logging.WARNING)

            cls._configured = True

    @classmethod
    def get_logger(cls, name: str) -> logging.Logger:
        cls.configure()
        return logging.getLogger(name)
