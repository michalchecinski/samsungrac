"""Read-only Samsung usage history, with unscaled counters of unknown units."""

import asyncio
import base64
import binascii
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import sqlite3
import time
from typing import Any

from .connection_raw import ConnectionRaw8888
from .exceptions import AuthError, CannotConnect
from .protocol_8888 import SamsungFileResponse

USAGE_POLL_INTERVAL = 15 * 60
USAGE_REQUEST_TIMEOUT = 10
USAGE_PATH = "/files/usage.db"
MAX_USAGE_ROWS = 10000
MAX_DATABASE_BYTES = 2 * 1024 * 1024


class UnsupportedUsageFormat(ValueError):
    """The response does not use the verified base64 SQLite schema."""


@dataclass(frozen=True)
class UsageSnapshot:
    """The latest stored row; units and timestamp timezone are unverified."""

    date: str
    power_usage: int
    running_time: int
    record_count: int


def parse_usage_history(body: bytes) -> UsageSnapshot | None:
    """Parse the observed format in memory, without modifying or opening files.

    All rows are validated. Decreases are retained as raw values, never turned
    into offsets or scaled energy totals; reset semantics remain unverified.
    """
    if not body or len(body) > MAX_DATABASE_BYTES:
        raise ValueError("Invalid usage response size")
    try:
        database = base64.b64decode(body.strip(), validate=True)
    except binascii.Error as exc:
        raise UnsupportedUsageFormat("Expected plain base64") from exc
    if not database.startswith(b"SQLite format 3\x00"):
        raise UnsupportedUsageFormat("Expected SQLite database")
    with closing(sqlite3.connect(":memory:")) as connection:
        try:
            connection.deserialize(database)
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA query_only = ON")
            # Bound work even for structurally valid but hostile/corrupt databases.
            ticks = 0

            def limit_work() -> int:
                nonlocal ticks
                ticks += 1
                return int(ticks > 1000)

            connection.set_progress_handler(limit_work, 1000)
            schema = connection.execute(
                "SELECT type, sql FROM sqlite_schema WHERE name = 'power_usage_table'"
            ).fetchone()
            expected = (
                "CREATE TABLE power_usage_table (date NUMERIC(12) primary key default 0, "
                "power_usage NUMERIC(14) default 0, running_time NUMERIC(14) default 0)"
            )
            if schema != ("table", expected):
                raise UnsupportedUsageFormat("Unsupported usage schema")
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("Corrupt usage database")
            rows = connection.execute(
                "SELECT date, power_usage, running_time FROM power_usage_table "
                "ORDER BY date LIMIT ?",
                (MAX_USAGE_ROWS + 1,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ValueError("Malformed usage database") from exc
    if len(rows) > MAX_USAGE_ROWS:
        raise ValueError("Usage history exceeds record limit")
    for date, power, runtime in rows:
        if any(type(value) is not int or value < 0 for value in (date, power, runtime)):
            raise ValueError("Invalid usage counter or date")
        if len(str(date)) != 10:
            raise ValueError("Invalid usage date")
        datetime.strptime(str(date), "%Y%m%d%H")
    if not rows:
        return None
    date, power, runtime = rows[-1]
    return UsageSnapshot(str(date), power, runtime, len(rows))


def advertises_usage_file(device_state: dict[str, Any] | None) -> bool:
    """Accept only the observed same-device resource, never arbitrary URLs."""
    if not isinstance(device_state, dict):
        return False
    resource = device_state.get("EnergyConsumption")
    return isinstance(resource, dict) and resource.get("saveLocation") == USAGE_PATH


@dataclass(frozen=True)
class UsageFileMetadata:
    """Safe metadata for the latest attempt; no inferred consumption values."""

    status: str = "not_requested"
    http_status: int | None = None
    byte_count: int | None = None
    sha256: str | None = None


def inspect_usage_response(response: SamsungFileResponse) -> UsageFileMetadata:
    """Describe original bytes without assuming their encoding or schema."""
    if response.status in (404, 410):
        return UsageFileMetadata("missing", response.status)
    if response.status != 200:
        return UsageFileMetadata("http_error", response.status)
    if not response.body:
        return UsageFileMetadata("empty", response.status)
    return UsageFileMetadata(
        "downloaded_unverified",
        response.status,
        len(response.body),
        hashlib.sha256(response.body).hexdigest(),
    )


class UsageHistoryReader:
    """Throttle optional reads independently of successful climate polls."""

    def __init__(self) -> None:
        self.metadata = UsageFileMetadata()
        self.snapshot: UsageSnapshot | None = None
        self._next_attempt = 0.0

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return metadata only, including an explicit lack of unit validation."""
        return {
            **asdict(self.metadata),
            "units_verified": False,
            "latest_record": asdict(self.snapshot) if self.snapshot else None,
        }

    def due(self, connection: Any, device_state: dict[str, Any] | None) -> bool:
        """Poll only the raw engine on devices advertising the observed file."""
        if not isinstance(connection, ConnectionRaw8888) or not advertises_usage_file(
            device_state
        ):
            self.metadata = UsageFileMetadata("unsupported")
            self.snapshot = None
            return False
        return time.monotonic() >= self._next_attempt

    async def async_read(
        self, connection: ConnectionRaw8888, network_lock: asyncio.Lock | None = None
    ) -> None:
        """Never propagate optional file/authentication failures to climate polling."""
        self._next_attempt = time.monotonic() + USAGE_POLL_INTERVAL
        self.snapshot = None
        try:
            async with network_lock if network_lock is not None else asyncio.Lock():
                async with asyncio.timeout(USAGE_REQUEST_TIMEOUT):
                    response = await connection.async_get_usage_file()
            self.metadata = inspect_usage_response(response)
            if self.metadata.status == "downloaded_unverified":
                # SQLite work runs outside the HA event loop and network lock.
                self.snapshot = await asyncio.to_thread(
                    parse_usage_history, response.body
                )
                self.metadata = UsageFileMetadata(
                    "parsed_raw" if self.snapshot else "empty_history",
                    response.status,
                    len(response.body),
                    self.metadata.sha256,
                )
        except UnsupportedUsageFormat:
            self.metadata = UsageFileMetadata("unsupported_format")
        except AuthError:
            self.metadata = UsageFileMetadata("unauthorized", 401)
        except TimeoutError, CannotConnect, OSError:
            self.metadata = UsageFileMetadata("read_failed")
        except ValueError, TypeError:
            self.metadata = UsageFileMetadata("malformed")
        except Exception:  # pylint: disable=broad-exception-caught
            # Never expose exception text, which can contain credentials or payloads.
            self.metadata = UsageFileMetadata("read_failed")
