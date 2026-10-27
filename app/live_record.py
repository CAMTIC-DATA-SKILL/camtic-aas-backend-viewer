"""실시간 프레임을 HDF7-9 와 같이 1초 간격 CSV 로 기록한다."""

from __future__ import annotations

import asyncio
import csv
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from utils.log_setup import LogSetup

from .live_frame import flatten_frame

logger = LogSetup.get_logger(__name__)

FetchFrame = Callable[[], Awaitable[dict[str, Any]]]


class LiveRecorder:
    def __init__(self, record_dir: Path, interval: float = 1.0) -> None:
        self.record_dir = record_dir
        self.interval = interval
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task[None]] = None
        self._state: dict[str, Any] = {
            "on": False,
            "rows": 0,
            "file": "",
            "packageId": "",
            "source": "",
        }

    def status(self) -> dict[str, Any]:
        return dict(self._state)

    async def set(
        self,
        on: bool,
        *,
        fetcher: Optional[FetchFrame] = None,
        package_id: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        async with self._lock:
            await self._cancel_task()
            if on:
                if fetcher is None:
                    raise ValueError("기록을 시작하려면 데이터 소스가 필요합니다.")
                name = f"{datetime.now():%Y-%m-%d}-data.csv"
                self._state.update(
                    on=True,
                    rows=0,
                    file=name,
                    packageId=package_id,
                    source=source,
                )
                self._task = asyncio.create_task(
                    self._run(fetcher, package_id, name),
                    name=f"live-record-{package_id}",
                )
            else:
                self._state["on"] = False
        return self.status()

    async def stop(self) -> dict[str, Any]:
        return await self.set(False)

    async def _cancel_task(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self, fetcher: FetchFrame, package_id: str, filename: str) -> None:
        columns: Optional[list[str]] = None
        last_t = 0.0
        last_seq: Any = object()
        folder = self.record_dir / (package_id or "default")
        try:
            while self._state["on"]:
                try:
                    frame = await fetcher()
                    now = time.monotonic()
                    seq = frame.get("seq")
                    same_seq = seq is not None and seq == last_seq
                    if now - last_t >= self.interval and not same_seq:
                        row = flatten_frame(frame)
                        if columns is None:
                            columns = list(row.keys())
                        path = folder / filename
                        _append_row(path, columns, row)
                        last_t = now
                        last_seq = seq
                        self._state["rows"] = int(self._state["rows"]) + 1
                        self._state["file"] = path.name
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("실시간 기록 실패")
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            logger.info("실시간 기록을 중지했습니다: %s", filename)
            raise


def _append_row(path: Path, columns: list[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(row)
