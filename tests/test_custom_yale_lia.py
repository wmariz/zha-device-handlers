"""Tests for the custom Yale LIA quirk: per-side unlock events."""

import importlib.util
from pathlib import Path
from unittest import mock

import pytest
from zigpy.zcl import foundation
from zigpy.zcl.clusters.closures import DoorLock
from zigpy.zcl.clusters.general import Basic

_PATH = Path(__file__).parent.parent / "custom_zha_quirks" / "yale_lia.py"
_spec = importlib.util.spec_from_file_location("custom_yale_lia", _PATH)
lia = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lia)  # registers the quirk


def _raw_device():
    import zigpy.device
    import zigpy.types as t

    app = mock.MagicMock()
    device = zigpy.device.Device(app, t.EUI64.convert("00:15:8d:00:8c:6e:98:5c"), 0x1111)
    device.manufacturer = "Yale"
    device.model = "LIA"
    ep = device.add_endpoint(1)
    ep.profile_id = 0x0104
    ep.device_type = 0x000A
    ep.add_input_cluster(Basic.cluster_id)
    ep.add_input_cluster(DoorLock.cluster_id)
    return device


@pytest.fixture
def lock():
    from zha.quirks import DEVICE_REGISTRY

    device = DEVICE_REGISTRY.resolve(_raw_device())
    cluster = device.endpoints[1].door_lock
    assert isinstance(cluster, lia.YaleLiaDoorLock)
    return cluster


def _events(lock, code, src, user):
    hdr = foundation.ZCLHeader.cluster(tsn=1, command_id=0x20)
    hdr.frame_control = hdr.frame_control.replace(
        direction=foundation.Direction.Server_to_Client
    )
    schema = lia.YaleLiaDoorLock.ClientCommandDefs.operation_event_notification.schema
    args = schema(
        operation_event_source=src, operation_event_code=code, user_id=user
    )
    with mock.patch.object(lock, "listener_event") as ev:
        lock.handle_cluster_request(hdr, args)
    return [(c.args[1], c.args[2]) for c in ev.call_args_list if c.args[0] == "zha_send_event"]


@pytest.mark.parametrize(
    ("code", "src", "user", "side"),
    [
        (0x0E, 0x02, 0, "unlock_inside"),  # thumbturn
        (0x02, 0x01, 0, "unlock_remote"),  # HA / app / voice
        (0x02, 0x00, 112, "unlock_outside"),  # PIN (Valdeci)
        (0x02, 0x03, 111, "unlock_outside"),  # card
        (0x02, 0x04, 111, "unlock_outside"),  # fingerprint
    ],
)
async def test_unlock_side_events(lock, code, src, user, side):
    events = _events(lock, code, src, user)
    assert events[0][0] == "lock_operation"
    assert events[1] == (side, {"user_id": user})


@pytest.mark.parametrize(("code", "src"), [(0x01, 0x02), (0x0A, 0x02), (0x0D, 0x02)])
async def test_locks_emit_no_side_event(lock, code, src):
    events = _events(lock, code, src, 0)
    assert [e[0] for e in events] == ["lock_operation"]
