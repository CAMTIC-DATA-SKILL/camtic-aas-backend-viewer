import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    cors_origins: list[str]


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("AASX_DATA_DIR", "data")).resolve()
    cors_origins = [
        origin.strip()
        for origin in os.environ.get("AASX_CORS_ORIGINS", "*").split(",")
        if origin.strip()
    ]
    return Settings(data_dir=data_dir, cors_origins=cors_origins)
