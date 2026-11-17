"""Raw usage sensor behavior, including independent availability."""

from pathlib import Path
from unittest.mock import MagicMock

from custom_components.climate_ip.connection_raw import ConnectionRaw8888
from custom_components.climate_ip.coordinator import SamsungClimateCoordinator
from custom_components.climate_ip.sensor import UsageCounterSensor, async_setup_entry
from custom_components.climate_ip.usage_history import (
    UsageHistoryReader,
    UsageSnapshot,
    parse_usage_history,
)


def coordinator_with_history():
    coordinator = MagicMock(spec=SamsungClimateCoordinator)
    coordinator.unique_id = "synthetic-device"
    coordinator.last_update_success = True
    coordinator.usage_history = UsageHistoryReader()
    coordinator.controller = MagicMock()
    coordinator.controller.connection = MagicMock(spec=ConnectionRaw8888)
    coordinator.controller.pure_device_state = {
        "EnergyConsumption": {"saveLocation": "/files/usage.db"}
    }
    coordinator.controller.device_state = {}
    coordinator.controller.sensors = []
    return coordinator


async def test_raw_sensors_exist_before_initial_download():
    coordinator = coordinator_with_history()
    entry = MagicMock(runtime_data=coordinator)
    entities = []
    await async_setup_entry(MagicMock(), entry, entities.extend)
    assert len(entities) == 2
    assert all(isinstance(entity, UsageCounterSensor) for entity in entities)
    assert all(
        entity.native_value is None and not entity.available for entity in entities
    )
    assert entities[0].unique_id == "synthetic-device_usage_history_power_usage"
    assert entities[1].unique_id == "synthetic-device_usage_history_running_time"


async def test_real_raw_values_have_no_energy_or_duration_classification():
    coordinator = coordinator_with_history()
    body = (Path(__file__).parent / "fixtures/tp6x_usage_base64.txt").read_bytes()
    coordinator.usage_history.snapshot = parse_usage_history(body)
    entities = []
    await async_setup_entry(
        MagicMock(), MagicMock(runtime_data=coordinator), entities.extend
    )
    assert [entity.native_value for entity in entities] == [9690, 19770]
    for entity in entities:
        assert entity.available
        assert entity.native_unit_of_measurement is None
        assert entity.device_class is None
        assert entity.state_class is None
        assert "(raw)" in entity.name
        assert entity.extra_state_attributes == {
            "latest_record_date": "2026111014",
            "date_format": "YYYYMMDDHH",
            "timezone_verified": False,
            "units_verified": False,
            "record_count": 177,
        }
    coordinator.usage_history.snapshot = None
    assert all(
        not entity.available and entity.native_value is None for entity in entities
    )
    assert (
        coordinator.last_update_success
    )  # Usage failure leaves climate availability alone.


async def test_raw_reset_value_is_not_accumulated():
    coordinator = coordinator_with_history()
    entities = []
    await async_setup_entry(
        MagicMock(), MagicMock(runtime_data=coordinator), entities.extend
    )
    coordinator.usage_history.snapshot = UsageSnapshot("2026111014", 1000, 2000, 1)
    assert entities[0].native_value == 1000
    coordinator.usage_history.snapshot = UsageSnapshot("2026111015", 1, 2, 2)
    assert entities[0].native_value == 1
    assert entities[1].native_value == 2
    coordinator.last_update_success = False
    assert not any(entity.available for entity in entities)


async def test_unsupported_device_gets_no_raw_sensors():
    coordinator = coordinator_with_history()
    coordinator.controller.pure_device_state = {}
    entities = []
    await async_setup_entry(
        MagicMock(), MagicMock(runtime_data=coordinator), entities.extend
    )
    assert entities == []
