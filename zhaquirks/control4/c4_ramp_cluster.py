"""C4 ramp/transition time + hardware-config cluster for Control4 dimmers.

Documented from the C4-APD120/LDZ-101 dimmer provisioning protocol (c4.dm.*
namespaces). During provisioning, the coordinator queries 9 transition time
parameters via Get commands. This cluster allows reading and writing those
parameters, plus two related hardware-config toggles, from Home Assistant
via ZHA service calls.

Transition time indices (from APD120 capture):
  Index 01:  100 ms  — fast/instant ramp
  Index 02:  750 ms  — on-ramp time  (used by C4DimmerOnOff for turn-on)
  Index 03: 2000 ms  — off-ramp time (used by C4DimmerOnOff for turn-off)
  Index 04: 5000 ms  — slow fade 1
  Index 05: 5000 ms  — slow fade 2
  Index 06:  100 ms  — fast ramp (secondary)
  Index 08:    0 ms  — disabled
  Index 09:    0 ms  — disabled
  Index 0A:    0 ms  — disabled

Ramp protocol:
  Get:  0g<seq4> c4.dm.tv <ch> <idx>
  Set:  0s<seq4> c4.dm.tv <ch> <idx> <value_hex_ms>

Values are unsigned 16-bit integers representing milliseconds.

CONFIRMED from a real HC300 controller log (SET_BUTTON_ATTACHED /
SET_LED_ATTACHED executing on the "Wireless Dimmer" driver — the LDZ-101
in Composer): two related hardware-config toggles, using their own
namespaces rather than c4.dm.tv's indexed-value shape:
  Set button-hardware-attached: 0s<seq4> c4.dm.ba <0|1>
  Set LED-hardware-attached:    0s<seq4> c4.dm.lm <0|1>
Both take a single decimal digit (0 or 1), not a hex-padded value — no Get
form observed. Added here rather than to C4LEDCluster/the button cluster
since Composer groups these with the ramp rates as one "Wireless Dimmer"
hardware-config panel, and the wire transport is identical.

button_attached/led_attached are exposed as writable local (manufacturer-
specific) Bool ATTRIBUTES rather than ZCL commands. A plain command never
gets its own HA entity and — being typed uint8_t — rendered as a 0-255
slider in the ZHA "Issue command" UI instead of a clean toggle. Attributes
get both a proper True/False control in the Cluster Attributes UI (Bool
type) and a real ZHA-generated Switch entity (in the same way a writable
LevelControl attribute like on_level gets auto-exposed as a Number
entity), which is what a hardware-config toggle should be. Writing the
attribute sends the wire command as a side effect and caches the result
locally, since no Get form exists to read the real value back.

Exported:
  C4RampCluster         — cluster with ramp-rate + hardware-config commands
  C4_RAMP_CLUSTER_ID    — cluster ID (0xFC44)
  RAMP_IDX_*            — named constants for transition time indices
"""

import asyncio
import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.quirks import CustomCluster
import zigpy.types as t
from zigpy.zcl import foundation
from zigpy.zcl.foundation import (
    BaseAttributeDefs,
    BaseCommandDefs,
    Status as ZCLStatus,
    ZCLAttributeDef,
    ZCLCommandDef,
)

from c4_helpers import (
    C4_PROFILE_BUTTON,
    C4_CLUSTER_ID,
    C4_PROVISION_DELAY,
    _build_c4_frame,
    next_c4_seq,
)

_LOGGER = logging.getLogger(__name__)

C4_RAMP_CLUSTER_ID = 0xFC44

# ---------------------------------------------------------------------------
# Transition time index constants
# ---------------------------------------------------------------------------
RAMP_IDX_FAST        = 0x01   # 100 ms default — instant/fast ramp
RAMP_IDX_ON          = 0x02   # 750 ms default — on-ramp time
RAMP_IDX_OFF         = 0x03   # 2000 ms default — off-ramp time
RAMP_IDX_SLOW_1      = 0x04   # 5000 ms default — slow fade 1
RAMP_IDX_SLOW_2      = 0x05   # 5000 ms default — slow fade 2
RAMP_IDX_FAST_2      = 0x06   # 100 ms default — fast ramp (secondary)
RAMP_IDX_DISABLED_1  = 0x08   # 0 ms — disabled
RAMP_IDX_DISABLED_2  = 0x09   # 0 ms — disabled
RAMP_IDX_DISABLED_3  = 0x0A   # 0 ms — disabled

# All known indices in provisioning order
RAMP_INDICES = [
    RAMP_IDX_FAST, RAMP_IDX_ON, RAMP_IDX_OFF,
    RAMP_IDX_SLOW_1, RAMP_IDX_SLOW_2, RAMP_IDX_FAST_2,
    RAMP_IDX_DISABLED_1, RAMP_IDX_DISABLED_2, RAMP_IDX_DISABLED_3,
]

# Default values (ms) observed from the device
RAMP_DEFAULTS_MS = {
    RAMP_IDX_FAST:       100,
    RAMP_IDX_ON:         750,
    RAMP_IDX_OFF:        2000,
    RAMP_IDX_SLOW_1:     5000,
    RAMP_IDX_SLOW_2:     5000,
    RAMP_IDX_FAST_2:     100,
    RAMP_IDX_DISABLED_1: 0,
    RAMP_IDX_DISABLED_2: 0,
    RAMP_IDX_DISABLED_3: 0,
}

# Friendly names for logging / UI
RAMP_INDEX_NAMES = {
    RAMP_IDX_FAST:       "fast",
    RAMP_IDX_ON:         "on_ramp",
    RAMP_IDX_OFF:        "off_ramp",
    RAMP_IDX_SLOW_1:     "slow_1",
    RAMP_IDX_SLOW_2:     "slow_2",
    RAMP_IDX_FAST_2:     "fast_2",
    RAMP_IDX_DISABLED_1: "disabled_1",
    RAMP_IDX_DISABLED_2: "disabled_2",
    RAMP_IDX_DISABLED_3: "disabled_3",
}


def _ms_to_zcl_tenths(ms: int) -> int:
    """Convert milliseconds to ZCL 1/10-second units."""
    return max(0, (ms + 50) // 100)  # round to nearest tenth


class C4RampCluster(CustomCluster):
    """Ramp/transition time + hardware-config cluster for Control4 dimmers.

    Provides commands to read and write dimmer ramp rates via the C4
    serial-over-ZigBee protocol (c4.dm.tv namespace), plus two related
    hardware-config toggles (button/LED attached — c4.dm.ba / c4.dm.lm)
    that Composer groups with the ramp rates under the same "Wireless
    Dimmer" driver panel.

    The cluster caches the current ramp times locally so that
    C4DimmerOnOff can read them for on/off transition commands.
    button_attached/led_attached are writable Bool attributes (see
    AttributeDefs below) rather than commands: writing one sends the
    corresponding wire command and caches the value locally, since no
    confirmed Get command exists for them. ZHA auto-generates a Switch
    entity for a writable Bool attribute on a quirk cluster, which is
    the point — a config toggle should be a real entity, not a manually
    issued cluster command.

    Usage from Home Assistant (via zha.issue_zigbee_cluster_command)
    for the ramp-rate commands:
      service: zha.issue_zigbee_cluster_command
      data:
        ieee: "00:0f:ff:..."
        endpoint_id: 4
        cluster_id: 0xFC44
        cluster_type: in
        command: 0          # set_ramp_rate
        command_type: server
        args:
          - 2               # index (RAMP_IDX_ON = on-ramp)
          - 1500            # time_ms (1500 ms)

    button_attached/led_attached are set by writing the attribute
    instead (e.g. via the ZHA "Clusters" -> Attributes UI, or the
    auto-generated Switch entity once the device reloads):
      service: zha.set_zigbee_cluster_attribute
      data:
        ieee: "00:0f:ff:..."
        endpoint_id: 4
        cluster_id: 0xFC44
        cluster_type: in
        attribute: 0        # button_attached (1 = led_attached)
        value: true
    """

    cluster_id = C4_RAMP_CLUSTER_ID
    name = "Control4 Ramp Control"
    ep_attribute = "c4_ramp_control"
    _c4_custom_handler = True

    # Local cache: index → time in ms
    _ramp_times: dict[int, int] = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Initialize cache with defaults
        self._ramp_times = dict(RAMP_DEFAULTS_MS)
        # Seed button/led attached to the real device's observed factory
        # default (both attached) so the entity shows a sane value before
        # any write ever happens, instead of "unknown".
        self._update_attribute(self.AttributeDefs.button_attached.id, True)
        self._update_attribute(self.AttributeDefs.led_attached.id, True)

    class AttributeDefs(BaseAttributeDefs):
        """Local, writable hardware-config attributes (no real ZCL Get)."""

        button_attached = ZCLAttributeDef(
            id=0x0000,
            type=t.Bool,
            access="rw",
            is_manufacturer_specific=True,
        )
        led_attached = ZCLAttributeDef(
            id=0x0001,
            type=t.Bool,
            access="rw",
            is_manufacturer_specific=True,
        )

    # attribute id -> (c4 wire namespace, log label)
    _ATTACHED_ATTRS = {
        0x0000: ("c4.dm.ba", "button_attached"),
        0x0001: ("c4.dm.lm", "led_attached"),
    }

    class ServerCommandDefs(BaseCommandDefs):
        """Server commands exposed to ZHA UI and service calls."""

        set_ramp_rate = ZCLCommandDef(
            id=0x00,
            schema={
                "index": t.uint8_t,
                "time_ms": t.uint16_t,
            },
            is_manufacturer_specific=True,
        )

        set_on_ramp = ZCLCommandDef(
            id=0x01,
            schema={
                "time_ms": t.uint16_t,
            },
            is_manufacturer_specific=True,
        )

        set_off_ramp = ZCLCommandDef(
            id=0x02,
            schema={
                "time_ms": t.uint16_t,
            },
            is_manufacturer_specific=True,
        )

        set_on_off_ramps = ZCLCommandDef(
            id=0x03,
            schema={
                "on_time_ms": t.uint16_t,
                "off_time_ms": t.uint16_t,
            },
            is_manufacturer_specific=True,
        )

    # ------------------------------------------------------------------
    # Attribute overrides — button_attached/led_attached (local-only,
    # side-effecting: a write sends the wire command then caches locally)
    # ------------------------------------------------------------------

    async def write_attributes(self, attributes, manufacturer=None):
        device_attrs = {}
        for attr, value in attributes.items():
            attr_def = self.find_attribute(attr) if isinstance(attr, str) else None
            attr_id = attr_def.id if attr_def is not None else attr
            if attr_id in self._ATTACHED_ATTRS:
                namespace, label = self._ATTACHED_ATTRS[attr_id]
                await self._send_attached_flag(namespace, label, bool(value))
                self._update_attribute(attr_id, bool(value))
            else:
                device_attrs[attr] = value

        if device_attrs:
            return await super().write_attributes(device_attrs, manufacturer=manufacturer)
        return [[foundation.WriteAttributesStatusRecord(ZCLStatus.SUCCESS)]]

    async def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None,
    ):
        local, device_attrs = {}, []
        for attr in attributes:
            attr_def = self.find_attribute(attr) if isinstance(attr, str) else None
            attr_id = attr_def.id if attr_def is not None else attr
            if attr_id in self._ATTACHED_ATTRS:
                local[attr_id] = self.get(attr_id, True)
            else:
                device_attrs.append(attr)

        success, failure = {}, {}
        if device_attrs:
            success, failure = await super().read_attributes(
                device_attrs,
                allow_cache=allow_cache,
                only_cache=only_cache,
                manufacturer=manufacturer,
            )
        success.update(local)
        return success, failure

    # ------------------------------------------------------------------
    # Public accessors for other clusters (C4DimmerOnOff)
    # ------------------------------------------------------------------

    def get_on_ramp_tenths(self) -> int:
        """Return the on-ramp time in ZCL 1/10-second units."""
        return _ms_to_zcl_tenths(self._ramp_times.get(RAMP_IDX_ON, 750))

    def get_off_ramp_tenths(self) -> int:
        """Return the off-ramp time in ZCL 1/10-second units."""
        return _ms_to_zcl_tenths(self._ramp_times.get(RAMP_IDX_OFF, 2000))

    def get_ramp_ms(self, index: int) -> int:
        """Return a ramp time in ms for a given index."""
        return self._ramp_times.get(index, RAMP_DEFAULTS_MS.get(index, 0))

    # ------------------------------------------------------------------
    # Command method overrides
    # ------------------------------------------------------------------

    async def set_ramp_rate(self, index, time_ms):
        """Set a specific transition time by index."""
        idx = int(index)
        ms = int(time_ms)
        if idx not in RAMP_INDICES:
            _LOGGER.warning(
                "C4 Ramp: invalid index 0x%02x (valid: %s)",
                idx, [f"0x{i:02x}" for i in RAMP_INDICES],
            )
            return
        await self._send_ramp_set(idx, ms)

    async def set_on_ramp(self, time_ms):
        """Set the on-ramp time (index 0x02)."""
        await self._send_ramp_set(RAMP_IDX_ON, int(time_ms))

    async def set_off_ramp(self, time_ms):
        """Set the off-ramp time (index 0x03)."""
        await self._send_ramp_set(RAMP_IDX_OFF, int(time_ms))

    async def set_on_off_ramps(self, on_time_ms, off_time_ms):
        """Set both on-ramp and off-ramp times in one call."""
        await self._send_ramp_set(RAMP_IDX_ON, int(on_time_ms))
        await self._send_ramp_set(RAMP_IDX_OFF, int(off_time_ms))

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        """Log any unexpected inbound cluster requests."""
        _LOGGER.debug(
            "C4 Ramp: cluster request cmd=0x%02x args=%s",
            hdr.command_id if hdr else -1, args,
        )

    # ------------------------------------------------------------------
    # C4 command builder and transport
    # ------------------------------------------------------------------

    async def _send_ramp_set(self, index: int, time_ms: int):
        """Send a c4.dm.tv Set command to change a transition time."""
        device = self.endpoint.device
        name = RAMP_INDEX_NAMES.get(index, f"0x{index:02x}")

        # Clamp to uint16 range
        time_ms = max(0, min(65535, time_ms))

        # Channel is always 00 for single-output dimmer
        seq = next_c4_seq(device)
        cmd = f"0s{seq:04x} c4.dm.tv 00 {index:02x} {time_ms:04x}"

        _LOGGER.info(
            "C4 Ramp: setting %s (idx 0x%02x) to %d ms — cmd: %s",
            name, index, time_ms, cmd,
        )

        frame = _build_c4_frame(seq, cmd)
        try:
            await device.request(
                profile=C4_PROFILE_BUTTON,
                cluster=C4_CLUSTER_ID,
                src_ep=1, dst_ep=1,
                sequence=device.get_sequence(),
                data=frame,
                expect_reply=False,
            )
            # Update local cache on successful send
            old_ms = self._ramp_times.get(index, 0)
            self._ramp_times[index] = time_ms
            _LOGGER.info(
                "C4 Ramp: %s updated: %d ms → %d ms (ZCL: %d tenths)",
                name, old_ms, time_ms, _ms_to_zcl_tenths(time_ms),
            )

            # Sync ZCL LevelControl transition attributes on EP 1
            self._sync_zcl_transition_attrs()

        except Exception as e:
            _LOGGER.warning(
                "C4 Ramp: failed to set %s to %d ms — %s", name, time_ms, e,
            )

    async def _send_attached_flag(self, namespace: str, label: str, attached: int):
        """Send a `0s<seq> <namespace> <0|1>` hardware-attached toggle.

        Shared transport for the button_attached/led_attached attribute
        writes (see write_attributes above) — same C4_PROFILE_BUTTON/
        C4_CLUSTER_ID/EP1->EP1 send as _send_ramp_set, just a single
        decimal 0/1 payload instead of an indexed hex value. The caller
        is responsible for updating the local attribute cache; this
        method only handles the wire transport, since no confirmed Get
        command exists to read the real value back.
        """
        device = self.endpoint.device
        attached = 1 if attached else 0
        seq = next_c4_seq(device)
        cmd = f"0s{seq:04x} {namespace} {attached:d}"

        _LOGGER.info(
            "C4 Ramp: setting %s=%d — cmd: %s", label, attached, cmd,
        )

        frame = _build_c4_frame(seq, cmd)
        try:
            await device.request(
                profile=C4_PROFILE_BUTTON,
                cluster=C4_CLUSTER_ID,
                src_ep=1, dst_ep=1,
                sequence=device.get_sequence(),
                data=frame,
                expect_reply=False,
            )
        except Exception as e:
            _LOGGER.warning(
                "C4 Ramp: failed to set %s=%d — %s", label, attached, e,
            )

    def _sync_zcl_transition_attrs(self):
        """Push cached ramp times into the EP 1 LevelControl attribute cache.

        This keeps ZHA's attribute display consistent with the actual device
        ramp times and ensures C4DimmerOnOff uses the correct values.
        """
        try:
            from zigpy.zcl.clusters.general import LevelControl

            ep1 = self.endpoint.device.endpoints.get(1)
            if ep1 is None:
                return
            level_cluster = ep1.in_clusters.get(LevelControl.cluster_id)
            if level_cluster is None:
                return

            on_tenths = self.get_on_ramp_tenths()
            off_tenths = self.get_off_ramp_tenths()

            level_cluster._update_attribute(
                LevelControl.AttributeDefs.on_transition_time.id, on_tenths,
            )
            level_cluster._update_attribute(
                LevelControl.AttributeDefs.off_transition_time.id, off_tenths,
            )
            level_cluster._update_attribute(
                LevelControl.AttributeDefs.on_off_transition_time.id, on_tenths,
            )
            _LOGGER.debug(
                "C4 Ramp: synced ZCL attrs — on=%d, off=%d (1/10 s)",
                on_tenths, off_tenths,
            )
        except Exception:
            _LOGGER.debug("C4 Ramp: ZCL attr sync failed", exc_info=True)
