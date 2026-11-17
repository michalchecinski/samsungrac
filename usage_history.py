"""Read-only discovery of opaque Samsung usage history.

No counter, encoding, units or reset semantics have been verified on port 8888.
Only download metadata is exposed; contents never enter HA state or diagnostics.
"""

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import time
from typing import Any

from .connection_raw import ConnectionRaw8888
from .exceptions import AuthError, CannotConnect
from .protocol_8888 import SamsungFileResponse

USAGE_POLL_INTERVAL = 15 * 60
USAGE_REQUEST_TIMEOUT = 10
USAGE_PATH = "/files/usage.db"


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
        self._next_attempt = 0.0

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return metadata only, including an explicit lack of unit validation."""
        return {**asdict(self.metadata), "units_verified": False}

    def due(self, connection: Any, device_state: dict[str, Any] | None) -> bool:
        """Poll only the raw engine on devices advertising the observed file."""
        if not isinstance(connection, ConnectionRaw8888) or not advertises_usage_file(
            device_state
        ):
            self.metadata = UsageFileMetadata("unsupported")
            return False
        return time.monotonic() >= self._next_attempt

    async def async_read(self, connection: ConnectionRaw8888) -> None:
        """Never propagate optional file/authentication failures to climate polling."""
        self._next_attempt = time.monotonic() + USAGE_POLL_INTERVAL
        try:
            async with asyncio.timeout(USAGE_REQUEST_TIMEOUT):
                response = await connection.async_get_usage_file()
            self.metadata = inspect_usage_response(response)
        except AuthError:
            self.metadata = UsageFileMetadata("unauthorized", 401)
        except TimeoutError, CannotConnect, OSError:
            self.metadata = UsageFileMetadata("read_failed")
        except ValueError, TypeError:
            self.metadata = UsageFileMetadata("malformed")
        except Exception:  # pylint: disable=broad-exception-caught
            # Never expose exception text, which can contain credentials or payloads.
            self.metadata = UsageFileMetadata("read_failed")
