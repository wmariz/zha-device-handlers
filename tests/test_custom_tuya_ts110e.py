"""Tests for the custom Tuya TS110E 2-gang dimmer quirk (custom_zha_quirks)."""

import importlib.util
from pathlib import Path
from unittest import mock

import pytest
from zigpy.zcl import ClusterType, foundation
from zigpy.zcl.clusters.general import LevelControl, OnOff

_PATH = Path(__file__).parent.parent / "custom_zha_quirks" / "tuya_ts110e_2gang.py"
_spec = importlib.util.spec_from_file_location("custom_tuya_ts110e_2gang", _PATH)
ts110e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ts110e)  # registers the quirk

MIN_RAW = 0xFC03
MAX_RAW = 0xFC04
MIN_PCT = 0xEF10
MAX_PCT = 0xEF11


def _raw_device():
    """Unquirked TS110E as zigpy sees it (2 endpoints with OnOff + Level)."""
    import zigpy.device
    import zigpy.types as t

    app = mock.MagicMock()
    device = zigpy.device.Device(app, t.EUI64.convert("a4:c1:38:00:00:00:00:01"), 0x1234)
    device.manufacturer = "_TZ3210_3mpwqzuu"
    device.model = "TS110E"
    for ep_id in (1, 2):
        ep = device.add_endpoint(ep_id)
        ep.profile_id = 0x0104
        ep.device_type = 0x0101
        ep.add_input_cluster(OnOff.cluster_id)
        ep.add_input_cluster(LevelControl.cluster_id)
    return device


@pytest.fixture
def level(request):
    """Level cluster of channel 1 of a quirked TS110E.

    Uses the zha-quirks 2.x registry (the one HA 2026.11 runs) when it is
    installed, else the repo's zigpy_device_from_v2_quirk fixture.
    """
    try:
        from zha.quirks import DEVICE_REGISTRY

        device = DEVICE_REGISTRY.resolve(_raw_device())
    except ImportError:
        factory = request.getfixturevalue("zigpy_device_from_v2_quirk")
        device = factory(
            "_TZ3210_3mpwqzuu",
            "TS110E",
            endpoint_ids=[1, 2],
            cluster_ids={
                ep: {
                    OnOff.cluster_id: ClusterType.Server,
                    LevelControl.cluster_id: ClusterType.Server,
                }
                for ep in (1, 2)
            },
        )
    cluster = device.endpoints[1].level
    assert isinstance(cluster, ts110e.TS110ELevelControl)
    return cluster


def _device(values: dict[int, int], accept_writes: bool = True):
    """Fake device storage for 0xFC03/0xFC04 with read/write mocks."""

    async def read(attributes, manufacturer=None):
        return (
            [
                foundation.ReadAttributeRecord(
                    a, foundation.Status.SUCCESS, foundation.TypeValue(None, values[a])
                )
                for a in attributes
            ],
        )

    async def write(attributes, manufacturer=None):
        if not accept_writes:
            return [
                [
                    foundation.WriteAttributesStatusRecord(
                        foundation.Status.INVALID_VALUE, a.attrid
                    )
                    for a in attributes
                ]
            ]
        for a in attributes:
            values[a.attrid] = a.value.value
        return [[foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS)]]

    return read, write


async def test_rejected_write_keeps_real_value(level):
    """A write the device rejects must not change what HA shows."""
    store = {MIN_RAW: 515, MAX_RAW: 767}
    read, write = _device(store, accept_writes=False)
    with (
        mock.patch.object(level, "_read_attributes", mock.AsyncMock(side_effect=read)),
        mock.patch.object(level, "_write_attributes", mock.AsyncMock(side_effect=write)),
    ):
        await level.write_attributes({"min_brightness": 100})

    assert store[MIN_RAW] == 515
    assert level.get(MIN_RAW) == 515
    assert level.get(MIN_PCT) == 52  # 51.5 % rounded, not the requested 100


async def test_accepted_write_is_read_back(level):
    """An accepted write ends with the value read back from the device."""
    store = {MIN_RAW: 515, MAX_RAW: 767}
    read, write = _device(store)
    read_mock = mock.AsyncMock(side_effect=read)
    with (
        mock.patch.object(level, "_read_attributes", read_mock),
        mock.patch.object(level, "_write_attributes", mock.AsyncMock(side_effect=write)),
    ):
        await level.write_attributes({"min_brightness": 99, "max_brightness": 100})

    assert store == {MIN_RAW: 990, MAX_RAW: 1000}
    assert level.get(MIN_PCT) == 99
    assert level.get(MAX_PCT) == 100
    assert read_mock.await_count == 1  # the read-back


async def test_cached_read_of_virtual_goes_live(level):
    """Even an only_cache read refreshes the virtual % from the device."""
    # stale pairing-time cache
    level._update_attribute(MIN_RAW, 11)
    level._update_attribute(MAX_RAW, 1000)
    assert level.get(MIN_PCT) == 1

    store = {MIN_RAW: 515, MAX_RAW: 767}
    read, _ = _device(store)
    with mock.patch.object(level, "_read_attributes", mock.AsyncMock(side_effect=read)):
        success, failure = await level.read_attributes(
            ["min_brightness", "max_brightness"], allow_cache=True, only_cache=True
        )

    assert failure == {}
    assert success == {"min_brightness": 52, "max_brightness": 77}


async def test_virtual_read_falls_back_to_cache_when_offline(level):
    """If the device does not answer, the cached value is served."""
    level._update_attribute(MIN_RAW, 990)
    with mock.patch.object(
        level, "_read_attributes", mock.AsyncMock(side_effect=TimeoutError)
    ):
        success, failure = await level.read_attributes(["min_brightness"])

    assert failure == {}
    assert success == {"min_brightness": 99}


@pytest.mark.parametrize(
    ("command", "sent", "expected"),
    [
        ("move_to_level_with_on_off", 254, 253),
        ("move_to_level", 254, 253),
        ("move_to_level_with_on_off", 200, 200),
    ],
)
async def test_level_capped_at_253(level, command, sent, expected):
    """Level 254 is sent as 253 (the firmware does not turn on at 254)."""
    with (
        mock.patch.object(level, "request", mock.AsyncMock()) as request,
        mock.patch.object(level.endpoint.on_off, "request", mock.AsyncMock()),
    ):
        await getattr(level, command)(level=sent, transition_time=0)
        await getattr(level, command)(sent, 0)

    for call in request.await_args_list:
        assert expected in call.args or call.kwargs.get("level") == expected


@pytest.mark.parametrize("call_style", ["kwargs", "args"])
async def test_level_with_on_off_sends_plain_on(level, call_style):
    """move_to_level_with_on_off -> move_to_level + OnOff.on() (the firmware
    applies the level of the combined command but leaves the output off)."""
    order = []
    lvl_req = mock.AsyncMock(side_effect=lambda *a, **k: order.append(("level", a[1])))
    on_req = mock.AsyncMock(side_effect=lambda *a, **k: order.append(("onoff", a[1])))
    with (
        mock.patch.object(level, "request", lvl_req),
        mock.patch.object(level.endpoint.on_off, "request", on_req),
    ):
        if call_style == "kwargs":
            await level.move_to_level_with_on_off(level=254, transition_time=0)
        else:
            await level.move_to_level_with_on_off(254, 0)

    move_to_level = LevelControl.ServerCommandDefs.move_to_level.id
    on = OnOff.ServerCommandDefs.on.id
    assert order == [("level", move_to_level), ("onoff", on)]
    call = lvl_req.await_args
    assert 253 in call.args or call.kwargs.get("level") == 253


async def test_level_with_on_off_zero_is_unchanged(level):
    """Level 0 (turn off) keeps the combined command and sends no on()."""
    lvl_req = mock.AsyncMock()
    on_req = mock.AsyncMock()
    with (
        mock.patch.object(level, "request", lvl_req),
        mock.patch.object(level.endpoint.on_off, "request", on_req),
    ):
        await level.move_to_level_with_on_off(level=0, transition_time=0)

    assert lvl_req.await_args.args[1] == LevelControl.ServerCommandDefs.move_to_level_with_on_off.id
    on_req.assert_not_awaited()


def _report(values: dict[int, int]):
    """Build a ZCL Report_Attributes header/args pair."""
    import zigpy.types as t

    hdr = foundation.ZCLHeader.general(
        tsn=1, command_id=foundation.GeneralCommand.Report_Attributes
    )
    args = foundation.GENERAL_COMMANDS[
        foundation.GeneralCommand.Report_Attributes
    ].schema(
        attribute_reports=[
            foundation.Attribute(a, foundation.TypeValue(t.uint16_t, v))
            for a, v in values.items()
        ]
    )
    return hdr, args


async def test_power_loss_report_restores_wanted(level):
    """After a power loss the device reports 515/767; the quirk restores."""
    import asyncio

    store = {MIN_RAW: 515, MAX_RAW: 767}
    read, write = _device(store)
    write_mock = mock.AsyncMock(side_effect=write)
    with (
        mock.patch.object(level, "_read_attributes", mock.AsyncMock(side_effect=read)),
        mock.patch.object(level, "_write_attributes", write_mock),
    ):
        await level.write_attributes({"max_brightness": 100, "min_brightness": 100})
        assert store == {MIN_RAW: 1000, MAX_RAW: 1000}
        assert level.get(0xEF20) == 1000 and level.get(0xEF21) == 1000

        # breaker cycled: the firmware forgets and reports its defaults
        store.update({MIN_RAW: 515, MAX_RAW: 767})
        write_mock.reset_mock()
        level._last_restore = 0.0
        with mock.patch.object(ts110e, "_RESTORE_STEP", 0):
            level.handle_cluster_general_request(*_report({MIN_RAW: 515, MAX_RAW: 767}))
            await asyncio.sleep(0)
            await level._restore_task

    assert store == {MIN_RAW: 1000, MAX_RAW: 1000}
    assert level.get(MIN_PCT) == 100 and level.get(MAX_PCT) == 100
    # one attribute per frame, max first (a single frame came back 767/767)
    frames = [[r.attrid for r in c.args[0]] for c in write_mock.await_args_list]
    assert frames == [[MAX_RAW], [MIN_RAW]]


async def test_report_equal_to_wanted_does_nothing(level):
    """A report matching the wanted values does not trigger a write."""
    store = {MIN_RAW: 100, MAX_RAW: 1000}
    read, write = _device(store)
    write_mock = mock.AsyncMock(side_effect=write)
    with (
        mock.patch.object(level, "_read_attributes", mock.AsyncMock(side_effect=read)),
        mock.patch.object(level, "_write_attributes", write_mock),
    ):
        await level.write_attributes({"min_brightness": 10, "max_brightness": 100})
        write_mock.reset_mock()
        level._last_restore = 0.0
        level.handle_cluster_general_request(*_report({MIN_RAW: 100, MAX_RAW: 1000}))

    assert level._restore_task is None or level._restore_task.done()
    write_mock.assert_not_awaited()


async def test_rejected_write_is_not_remembered(level):
    """A rejected write must not become the wanted value."""
    store = {MIN_RAW: 515, MAX_RAW: 767}
    read, write = _device(store, accept_writes=False)
    with (
        mock.patch.object(level, "_read_attributes", mock.AsyncMock(side_effect=read)),
        mock.patch.object(level, "_write_attributes", mock.AsyncMock(side_effect=write)),
    ):
        await level.write_attributes({"min_brightness": 100})
    assert level.get(0xEF20) is None


async def test_silent_recalibration_after_power_up_is_undone(level):
    """The device reports stored values at power-up, then silently drops
    the channel to 767/767; the delayed re-check restores the wanted ones."""
    import asyncio

    store = {MIN_RAW: 1000, MAX_RAW: 1000}
    read, write = _device(store)
    with (
        mock.patch.object(level, "_read_attributes", mock.AsyncMock(side_effect=read)),
        mock.patch.object(level, "_write_attributes", mock.AsyncMock(side_effect=write)),
        mock.patch.object(ts110e, "_RECHECK_DELAYS", (0, 0)),
        mock.patch.object(ts110e, "_RESTORE_STEP", 0),
    ):
        await level.write_attributes({"max_brightness": 100, "min_brightness": 100})

        # power-up report matches the wanted values: no immediate restore
        level.handle_cluster_general_request(*_report({MIN_RAW: 1000, MAX_RAW: 1000}))
        assert level._recheck_task is not None
        # ...but the firmware then re-calibrates silently
        store.update({MIN_RAW: 767, MAX_RAW: 767})
        await level._recheck_task

    assert store == {MIN_RAW: 1000, MAX_RAW: 1000}
    assert level.get(MIN_PCT) == 100 and level.get(MAX_PCT) == 100
