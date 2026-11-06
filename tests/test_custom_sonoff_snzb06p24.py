"""Tests for the custom SONOFF SNZB-06P24 quirk (custom_zha_quirks)."""

import asyncio
import importlib.util
from pathlib import Path
from unittest import mock

import pytest
from zigpy.zcl.clusters.general import Basic
from zigpy.zcl.clusters.measurement import IlluminanceMeasurement, OccupancySensing

_PATH = Path(__file__).parent.parent / "custom_zha_quirks" / "sonoff_snzb06p24.py"
_spec = importlib.util.spec_from_file_location("custom_sonoff_snzb06p24", _PATH)
snzb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(snzb)  # registers the quirk

MEASURED = IlluminanceMeasurement.AttributeDefs.measured_value.id
OCC = OccupancySensing.AttributeDefs.occupancy.id


def _raw_device():
    import zigpy.device
    import zigpy.types as t

    app = mock.MagicMock()
    device = zigpy.device.Device(app, t.EUI64.convert("a4:c1:38:1a:ba:4e:ff:ff"), 0xB18D)
    device.manufacturer = "SONOFF"
    device.model = "SNZB-06P24"
    ep = device.add_endpoint(1)
    ep.profile_id = 0x0104
    ep.device_type = 0x0107
    for cid in (Basic.cluster_id, IlluminanceMeasurement.cluster_id,
                OccupancySensing.cluster_id, 0xFC11):
        ep.add_input_cluster(cid)
    return device


@pytest.fixture
def ep():
    from zha.quirks import DEVICE_REGISTRY

    device = DEVICE_REGISTRY.resolve(_raw_device())
    ep = device.endpoints[1]
    assert isinstance(ep.occupancy, snzb.SNZB06P24Occupancy)
    assert isinstance(ep.illuminance, snzb.SNZB06P24Illuminance)
    return ep


async def test_occupancy_change_refreshes_lux_twice(ep):
    """Each occupancy change reads lux live (2 reads)."""
    read = mock.AsyncMock(return_value=({MEASURED: 1}, {}))
    with (
        mock.patch.object(ep.illuminance, "read_attributes", read),
        mock.patch.object(snzb, "_LUX_READ_DELAYS", (0, 0)),
    ):
        ep.occupancy._update_attribute(OCC, 1)
        await ep.occupancy._lux_task

    assert read.await_count == 2
    assert read.await_args.args[0] == [MEASURED]
    assert read.await_args.kwargs["allow_cache"] is False


async def test_same_occupancy_does_not_refresh(ep):
    """A repeated report with the same value does not trigger reads."""
    ep.occupancy._update_attribute(OCC, 1)
    if ep.occupancy._lux_task:
        ep.occupancy._lux_task.cancel()
    read = mock.AsyncMock(return_value=({}, {}))
    with mock.patch.object(ep.illuminance, "read_attributes", read):
        task_before = ep.occupancy._lux_task
        ep.occupancy._update_attribute(OCC, 1)
        assert ep.occupancy._lux_task is task_before
    read.assert_not_awaited()


async def test_new_change_cancels_pending_refresh(ep):
    """A fast on->off cancels the first refresh and starts a new one."""
    with (
        mock.patch.object(ep.illuminance, "read_attributes", mock.AsyncMock()),
        mock.patch.object(snzb, "_LUX_READ_DELAYS", (10, 10)),
    ):
        ep.occupancy._update_attribute(OCC, 1)
        first = ep.occupancy._lux_task
        ep.occupancy._update_attribute(OCC, 0)
        await asyncio.sleep(0)
        assert first.cancelled()
        ep.occupancy._lux_task.cancel()


async def test_lux_reporting_min_interval_capped(ep):
    """ZHA's 30 s minimum interval for lux becomes 5 s; others untouched."""
    with mock.patch.object(
        IlluminanceMeasurement, "configure_reporting_multiple", mock.AsyncMock()
    ) as parent:
        await ep.illuminance.configure_reporting_multiple(
            {"measured_value": (30, 900, 1)}
        )
    assert parent.await_args.args[0] == {"measured_value": (5, 900, 1)}

    with mock.patch.object(
        IlluminanceMeasurement, "configure_reporting", mock.AsyncMock()
    ) as parent:
        await ep.illuminance.configure_reporting(MEASURED, 30, 900, 1)
    assert parent.await_args.args[0:3] == (MEASURED, 5, 900)
