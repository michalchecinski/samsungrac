"""Usage discovery tests; binary samples are synthetic, not device captures."""

import asyncio
import base64
import json
import logging
from pathlib import Path
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_ip.connection_raw import ConnectionRaw8888
from custom_components.climate_ip.coordinator import SamsungClimateCoordinator
from custom_components.climate_ip.exceptions import AuthError, CannotConnect
from custom_components.climate_ip.protocol_8888 import (
    MAX_FILE_BYTES,
    Samsung8888Client,
    SamsungFileResponse,
)
from custom_components.climate_ip.usage_history import (
    UsageHistoryReader,
    advertises_usage_file,
    inspect_usage_response,
    parse_usage_history,
)


def real_usage_body():
    return (Path(__file__).parent / "fixtures/tp6x_usage_base64.txt").read_bytes()


def synthetic_usage_body(rows, schema=None):
    """Build counter/reset/malformed cases, explicitly separate from real evidence."""
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            schema
            or "CREATE TABLE power_usage_table (date NUMERIC(12) primary key default 0, power_usage NUMERIC(14) default 0, running_time NUMERIC(14) default 0)"
        )
        if rows:
            connection.executemany(
                "INSERT INTO power_usage_table VALUES (?, ?, ?)", rows
            )
        connection.commit()
        return base64.b64encode(connection.serialize())


def test_real_sanitized_advertisement():
    device = json.loads(
        (Path(__file__).parent / "fixtures/tp6x_device.txt").read_text()
    )
    assert device["description"] == "TP6X_RAC_17K"
    assert advertises_usage_file(device)
    # The option is present, but is never used as a counter.
    assert "UsagesDB_254" in device["Mode"]["options"]
    assert UsageHistoryReader().diagnostics["units_verified"] is False


@pytest.mark.parametrize(
    "state",
    [
        None,
        {},
        {"EnergyConsumption": None},
        {"EnergyConsumption": {"saveLocation": "https://other-host/files/usage.db"}},
        {"EnergyConsumption": {"saveLocation": "/files/../configuration"}},
    ],
)
def test_unsupported_advertisements(state):
    assert not advertises_usage_file(state)


async def file_client(body, *, status=200, extra_headers=b"", length=None):
    client = Samsung8888Client("ac.invalid")
    reader = asyncio.StreamReader()
    framing = b"" if length is None else f"Content-Length: {length}\r\n".encode()
    reader.feed_data(
        f"HTTP/1.1 {status} Result\r\n".encode()
        + framing
        + extra_headers
        + b"\r\n"
        + body
    )
    reader.feed_eof()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    client._reader, client._writer = reader, writer
    return client


async def test_preserves_binary_and_next_climate_response(caplog):
    body = b"\x00\xff\xfe\x80opaque\r\n{}"
    climate_body = b'{"Devices": []}'
    next_response = b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n" + climate_body
    client = await file_client(body + next_response, length=len(body))
    with caplog.at_level(logging.DEBUG):
        response = await client.request_file(
            "/files/usage.db", {"Authorization": "Bearer synthetic-secret"}
        )
    assert response == SamsungFileResponse(200, (f"Content-Length: {len(body)}",), body)
    assert "opaque" not in caplog.text
    assert "synthetic-secret" not in caplog.text
    assert inspect_usage_response(response).byte_count == len(body)
    # The next response is still framed and usable on the same socket.
    assert await client.request("GET", "/devices") == (climate_body.decode(), None)
    await client.close()


@pytest.mark.parametrize(
    "length,headers",
    [
        (None, b""),
        (-1, b""),
        ("invalid", b""),
        (MAX_FILE_BYTES + 1, b""),
        (3, b"Transfer-Encoding: chunked\r\n"),
        (3, b"Content-Length: 4\r\n"),
    ],
)
async def test_rejects_unknown_or_unsafe_framing_and_closes(length, headers):
    client = await file_client(b"abc", length=length, extra_headers=headers)
    with pytest.raises(CannotConnect):
        await client.request_file("/files/usage.db")
    assert client._writer is None
    assert client._reader is None


async def test_truncated_file_is_not_accepted():
    client = await file_client(b"abc", length=10)
    # The transport's one reconnect retry must fail, never return a partial body.
    with patch.object(
        client, "connect", AsyncMock(side_effect=[None, CannotConnect("offline")])
    ):
        with pytest.raises(CannotConnect):
            await client.request_file("/files/usage.db")
    assert client._reader is None


async def test_file_timeout_closes_socket():
    client = await file_client(b"abc", length=3)
    with patch.object(client, "_read_file_body", AsyncMock(side_effect=TimeoutError)):
        with pytest.raises(CannotConnect, match="Timeout"):
            await client.request_file("/files/usage.db")
    assert client._writer is None


async def test_file_unauthorized_closes_socket():
    client = await file_client(b"abc", status=401, length=3)
    with pytest.raises(AuthError):
        await client.request_file("/files/usage.db")
    assert client._writer is None


async def test_cancelling_file_read_closes_shared_socket():
    client = await file_client(b"", length=3)
    started = asyncio.Event()

    async def stalled_body(*args):
        started.set()
        await asyncio.Future()

    with patch.object(client, "_read_file_body", stalled_body):
        task = asyncio.create_task(client.request_file("/files/usage.db"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert client._reader is None
    assert client._writer is None


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (404, b"not found", "missing"),
        (410, b"", "missing"),
        (500, b"synthetic-secret", "http_error"),
        (204, b"", "http_error"),
        (200, b"", "empty"),
        (200, b"\xff", "downloaded_unverified"),
    ],
)
def test_response_metadata(status, body, expected):
    result = inspect_usage_response(
        SamsungFileResponse(status, ("Set-Cookie: synthetic-secret",), body)
    )
    assert result.status == expected
    assert "synthetic-secret" not in str(result)
    if expected != "downloaded_unverified":
        assert result.byte_count is None
        assert result.sha256 is None


async def test_current_auth_and_shared_client_without_embedded_commands():
    connection = ConnectionRaw8888(
        {"ip_address": "ac.invalid", "token": "synthetic-secret"},
        logging.getLogger(__name__),
    )
    client = MagicMock()
    client.request_file = AsyncMock(return_value=SamsungFileResponse(200, (), b"\xff"))
    with (
        patch.object(connection, "async_get_client", AsyncMock(return_value=client)),
        patch.object(
            connection, "_async_handle_embedded_command", AsyncMock()
        ) as embedded,
    ):
        await connection.async_get_usage_file()
    client.request_file.assert_awaited_once_with(
        "/files/usage.db",
        {
            "Authorization": "Bearer synthetic-secret",
            "Content-Type": "application/json",
        },
    )
    embedded.assert_not_awaited()
    assert connection.is_available is False


async def test_usage_download_holds_connection_lock():
    connection = ConnectionRaw8888(
        {"ip_address": "ac.invalid", "token": "synthetic-secret"},
        logging.getLogger(__name__),
    )
    client = MagicMock()

    async def read_file(*args):
        assert connection.async_lock.locked()
        return SamsungFileResponse(200, (), b"file")

    client.request_file = read_file
    with patch.object(connection, "async_get_client", AsyncMock(return_value=client)):
        await connection.async_get_usage_file()
    assert not connection.async_lock.locked()


@pytest.mark.parametrize(
    "failure,expected",
    [
        (AuthError("synthetic-secret"), "unauthorized"),
        (CannotConnect("synthetic-secret"), "read_failed"),
        (TimeoutError(), "read_failed"),
        (OSError(), "read_failed"),
        (ValueError(), "malformed"),
        (RuntimeError("synthetic-secret"), "read_failed"),
    ],
)
async def test_failure_clears_metadata_and_throttles_without_climate_failure(
    failure, expected
):
    reader = UsageHistoryReader()
    connection = MagicMock(spec=ConnectionRaw8888)
    connection.async_get_usage_file = AsyncMock(
        return_value=SamsungFileResponse(200, (), real_usage_body())
    )
    await reader.async_read(connection)
    assert reader.snapshot.power_usage == 9690
    connection.async_get_usage_file.side_effect = failure
    await reader.async_read(connection)
    assert reader.metadata.status == expected
    assert reader.metadata.byte_count is None  # Unknown, never fabricated zero.
    assert reader.snapshot is None
    assert not reader.due(
        connection, {"EnergyConsumption": {"saveLocation": "/files/usage.db"}}
    )
    assert "synthetic-secret" not in str(reader.diagnostics)


async def test_decrease_is_exposed_raw_without_invented_reset_compensation():
    reader = UsageHistoryReader()
    connection = MagicMock(spec=ConnectionRaw8888)
    connection.async_get_usage_file = AsyncMock(
        side_effect=[
            SamsungFileResponse(
                200, (), synthetic_usage_body([(2026111014, 1000, 2000)])
            ),
            SamsungFileResponse(
                200,
                (),
                synthetic_usage_body([(2026111014, 1000, 2000), (2026111015, 1, 2)]),
            ),
        ]
    )
    await reader.async_read(connection)
    first = reader.metadata.sha256
    assert reader.snapshot.power_usage == 1000
    await reader.async_read(connection)
    assert reader.metadata.sha256 != first
    assert reader.metadata.status == "parsed_raw"
    assert reader.snapshot.power_usage == 1
    assert reader.snapshot.running_time == 2
    assert reader.diagnostics["units_verified"] is False


async def test_conservative_poll_interval_and_unsupported_engine():
    reader = UsageHistoryReader()
    state = {"EnergyConsumption": {"saveLocation": "/files/usage.db"}}
    connection = MagicMock(spec=ConnectionRaw8888)
    connection.async_get_usage_file = AsyncMock(
        return_value=SamsungFileResponse(404, (), b"")
    )
    with patch(
        "custom_components.climate_ip.usage_history.time.monotonic", return_value=0
    ):
        assert reader.due(connection, state)
        await reader.async_read(connection)
    with patch(
        "custom_components.climate_ip.usage_history.time.monotonic", return_value=899
    ):
        assert not reader.due(connection, state)
    with patch(
        "custom_components.climate_ip.usage_history.time.monotonic", return_value=900
    ):
        assert reader.due(connection, state)
    assert not reader.due(object(), state)
    assert reader.metadata.status == "unsupported"


async def test_usage_failure_does_not_fail_coordinator_state_update():
    # Exercise the actual coordinator boundary without starting an HA instance.
    coordinator = object.__new__(SamsungClimateCoordinator)
    coordinator._global_network_lock = asyncio.Lock()
    coordinator.controller = MagicMock()
    coordinator.controller.async_get_status = AsyncMock()
    coordinator.usage_history = UsageHistoryReader()
    connection = MagicMock(spec=ConnectionRaw8888)
    connection.async_get_usage_file = AsyncMock(side_effect=CannotConnect("offline"))
    coordinator.controller.connection = connection
    coordinator.controller.pure_device_state = {
        "EnergyConsumption": {"saveLocation": "/files/usage.db"}
    }
    coordinator.config_entry = MagicMock()
    tasks = []
    coordinator.config_entry.async_create_background_task.side_effect = (
        lambda hass, coro, name: tasks.append(asyncio.create_task(coro)) or tasks[-1]
    )
    coordinator.hass = MagicMock()
    coordinator._usage_task = None
    coordinator.async_update_listeners = MagicMock()
    with patch.object(
        coordinator, "_create_device_state", return_value="climate-state"
    ):
        assert await coordinator._async_update_data() == "climate-state"
        await tasks[0]
        assert coordinator.usage_history.metadata.status == "read_failed"
        assert await coordinator._async_update_data() == "climate-state"
    assert len(tasks) == 1
    coordinator.controller.clear_state_cache.assert_not_called()
    assert coordinator.async_update_listeners.call_count == 1


def test_real_database_parsing_without_scaling():
    snapshot = parse_usage_history(real_usage_body())
    assert snapshot.date == "2026111014"
    assert snapshot.power_usage == 9690
    assert snapshot.running_time == 19770
    assert snapshot.record_count == 177


@pytest.mark.parametrize(
    "body",
    [
        b"invalid base64!",
        b'{"database": "abc"}',
        b"SQLite format 3\x00",
        base64.b64encode(b"not sqlite"),
        base64.b64encode(b"SQLite format 3\x00broken"),
    ],
)
def test_malformed_and_unknown_formats(body):
    with pytest.raises(ValueError):
        parse_usage_history(body)


@pytest.mark.parametrize(
    "row",
    [
        (2026130112, 1, 2),
        (0, 1, 2),
        (2026111014, -1, 2),
        (2026111014, None, 2),
        (2026111014, 1.25, 2),
        (2026111014, "unknown", 2),
    ],
)
def test_invalid_records(row):
    with pytest.raises(ValueError):
        parse_usage_history(synthetic_usage_body([row]))


def test_unknown_schema_and_empty_history():
    assert parse_usage_history(synthetic_usage_body([])) is None
    with pytest.raises(ValueError):
        parse_usage_history(
            synthetic_usage_body([], "CREATE TABLE unrelated (date, power, runtime)")
        )


def test_row_limit_and_invalid_historical_row():
    body = synthetic_usage_body([(2026111014, 1, 2), (2026111015, 3, 4)])
    with patch("custom_components.climate_ip.usage_history.MAX_USAGE_ROWS", 1):
        with pytest.raises(ValueError, match="record limit"):
            parse_usage_history(body)
    with pytest.raises(ValueError):
        parse_usage_history(
            synthetic_usage_body([(2026111014, -1, 2), (2026111015, 3, 4)])
        )


async def test_parser_runs_after_releasing_network_lock():
    reader = UsageHistoryReader()
    network_lock = asyncio.Lock()
    connection = MagicMock(spec=ConnectionRaw8888)

    async def download():
        assert network_lock.locked()
        return SamsungFileResponse(200, (), real_usage_body())

    def parse(body):
        assert not network_lock.locked()
        return parse_usage_history(body)

    connection.async_get_usage_file = download
    with patch("custom_components.climate_ip.usage_history.parse_usage_history", parse):
        await reader.async_read(connection, network_lock)
    assert reader.snapshot.power_usage == 9690


async def test_failed_parser_clears_latest_record():
    reader = UsageHistoryReader()
    connection = MagicMock(spec=ConnectionRaw8888)
    connection.async_get_usage_file = AsyncMock(
        side_effect=[
            SamsungFileResponse(200, (), real_usage_body()),
            SamsungFileResponse(200, (), b"unknown"),
        ]
    )
    await reader.async_read(connection)
    assert reader.snapshot is not None
    await reader.async_read(connection)
    assert reader.snapshot is None
    assert reader.metadata.status == "unsupported_format"


async def test_connection_close_header_is_honored():
    client = await file_client(b"abc", length=3, extra_headers=b"Connection: close\r\n")
    response = await client.request_file("/files/usage.db")
    assert response.body == b"abc"
    assert client._writer is None
