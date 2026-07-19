from __future__ import annotations

import fcntl
import os
from pathlib import Path

from sqlalchemy.engine import make_url


class ServiceInstanceLock:
    """Keep one CandlePilot service attached to a file-backed SQLite database."""

    def __init__(self, database_url: str) -> None:
        url = make_url(database_url)
        database = url.database
        self.path = (
            Path(f"{Path(database).expanduser().resolve()}.serve.lock")
            if url.drivername.startswith("sqlite")
            and database is not None
            and database != ":memory:"
            else None
        )
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self.path is None or self._descriptor is not None:
            return
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise RuntimeError(
                f"another CandlePilot service is already using database {self.path.name.removesuffix('.serve.lock')}"
            ) from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"pid={os.getpid()}\n".encode())
        self._descriptor = descriptor

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

