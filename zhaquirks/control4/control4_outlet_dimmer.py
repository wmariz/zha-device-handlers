"""ZHA quirk for the Control4 LOZ-5D1-W Dimming Outlet.

CONFIRMED on real hardware: outlet 1 (EP1) dims correctly — turning on at
an arbitrary brightness works, and dragging the brightness slider while
the light is on updates the level in place (it does not revert to off).
The Control4 controller's own app also shows outlet 2 as dimmable, so this
version gives it graduated brightness too, using a different transport
than outlet 1 (see below) since it has no real endpoint of its own.

History — why the two outlets use two different transports:

  Attempts 1 and 2 (see git history: commit 438bb9a, and the commit that
  replaced it) translated LevelControl commands for BOTH outlets into the
  outlet's c4.dm.tv text SET command with a graduated 0-100 level, exactly
  like C4OutletOnOff does for on/off in control4_outlet.py. On real
  hardware this made outlet 1 revert to (or stay) off for any level other
  than 0x00/0x64, and a plain turn-on got stuck at C4_DEFAULT_ON_LEVEL
  (191 -> 75%). At the time this looked like a protocol-wide dead end, but
  it turned out to only be true for outlet 1: both outlets were sharing
  the *same* broken assumption, and the failure was only ever demonstrated
  against outlet 1.

  Attempt 3 fixed outlet 1 by re-reading C4DimmerOnOff's docstring in
  control4_dimmer.py ("C4 dimmers ignore standard On/Off (cluster 0x0006)
  commands but respond to move_to_level_with_on_off (cluster 0x0008, cmd
  0x04)") — the real C4-APD120 gets genuine ZCL Level Control frames, not
  a synthesized text command. The LOZ-5S1-W's own interview already
  reports a native LevelControl cluster on EP1 (see control4_outlet.py's
  docstring: "clusters [Basic … OnOff Level Time]") that the switch quirk
  never wires up, because the switch doesn't dim. Outlet 1's dimming
  circuit turned out to behave like the APD120, and real ZCL passthrough
  was confirmed working.

  Outlet 2 has no such option: it is a synthetic endpoint (EP11) with no
  real Zigbee endpoint behind it (the device's interview only ever shows
  EP1/2/196/197/198), so there is nowhere to send a real ZCL frame — ZCL
  addressing has no field for "which physical circuit" within one
  endpoint/cluster. Since outlet 2 is confirmed dimmable in the original
  Control4 app, and the only channel that can address it at all is the
  outlet-index field in c4.dm.tv, this version gives outlet 2 back its
  graduated c4.dm.tv level (identical to attempts 1/2's approach) on the
  theory that it was never actually broken — only outlet 1 was, because it
  was wrongly using this same transport instead of real ZCL.

Implementation:
  • Outlet 1 (EP1) reuses C4DimmerOnOff / C4DimmerLevelControl UNCHANGED
    from control4_dimmer.py — no override, no text-command translation.
    EP2/EP196 reuse the base C4ConfigCluster (not C4OutletConfigCluster),
    matching the APD120's raw 0-255 dim-level report path
    (_sync_ep1_level) instead of the outlet's on/off-flag interpretation.
  • Outlet 2 (synthetic EP11) reuses C4DimmerOnOff UNCHANGED for the
    on/off->level redirect, paired with C4Outlet2DimmerLevelControl (this
    file), which sends c4.dm.tv <01> 00 <level> with a graduated 0-100
    level instead of the fixed 0x64/0x00 used for plain on/off.
  • EP197's button/state cluster treats outlet-index-1 c4.dm.tc
    announcements as a graduated level for outlet 2, and defers everything
    else (including any outlet-index-0 announcements) to the base
    on/off-only sync, so outlet 1's current_level stays owned exclusively
    by the real-ZCL / EP2-EP196 path above.

STILL UNVERIFIED — please re-test and report back specifically for outlet
2: turn it on at an arbitrary brightness, then drag the slider while it's
on, the same way outlet 1 was already confirmed.
"""

import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.profiles import zha
from zigpy.quirks import CustomDevice
from zigpy.zcl import foundation
from zigpy.zcl.foundation import Status as ZCLStatus
from zigpy.zcl.clusters.general import (
    Basic, Groups, Identify, LevelControl, OnOff, Scenes, Time,
)

from zhaquirks.const import (
    CLUSTER_ID,
    COMMAND,
    DEVICE_TYPE,
    ENDPOINT_ID,
    ENDPOINTS,
    INPUT_CLUSTERS,
    MODELS_INFO,
    OUTPUT_CLUSTERS,
    PROFILE_ID,
    SKIP_CONFIGURATION,
)

# Ensure patches are installed before this device class is used
import c4_hooks

from c4_helpers import (
    C4_BUTTON_CLUSTER_ID,
    C4_CLUSTER_ID,
    C4_MANUF_CLUSTER,
    C4_PROFILE_BUTTON,
    C4_PROFILE_NETWORK,
    C4_PROFILE_OUTLET,
    OUTLET_EP_MAP,
    C4ConfigCluster,
    C4DimmerManufCluster,
    _build_c4_frame,
    next_c4_seq,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import C4DualOutletButtonCluster
from c4_hooks import _C4_MODEL_QUIRK_MAP

# Reused UNCHANGED for outlet 1: real-ZCL transport, same as the C4-APD120
# (see module docstring). Also reused UNCHANGED for outlet 2's on/off->level
# redirect — it doesn't care what class implements self.endpoint.level.
from control4_dimmer import C4DimmerOnOff, C4DimmerLevelControl
from control4_outlet import C4OutletStateCluster

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Level scaling — ZCL Level Control (0-254) <-> C4 protocol percentage (0-100)
# Only used for outlet 2, which has no real endpoint of its own.
# ---------------------------------------------------------------------------

def _zcl_level_to_c4_pct(level_zcl: int) -> int:
    """Convert a ZCL Level Control value (0-254) to the C4 0-100 scale."""
    level_zcl = max(0, min(254, int(level_zcl)))
    return round(level_zcl * 100 / 254)


def _c4_pct_to_zcl_level(level_pct: int) -> int:
    """Convert a C4 protocol level (0-100) to a ZCL Level Control value."""
    level_pct = max(0, min(100, int(level_pct)))
    return round(level_pct * 254 / 100)


# ---------------------------------------------------------------------------
# Outlet 2 LevelControl — no real endpoint, so it speaks c4.dm.tv with a
# graduated level instead of the real ZCL frame outlet 1 uses.
# ---------------------------------------------------------------------------

class C4Outlet2DimmerLevelControl(C4DimmerLevelControl):
    """LevelControl for outlet 2 (synthetic EP11), via graduated c4.dm.tv.

    Inherits C4DimmerLevelControl's local caching of on_level/transition-time
    attributes but overrides write_attributes (never forward to the device —
    this protocol has no ZCL WriteAttributes equivalent at all) and
    move_to_level(_with_on_off) to send c4.dm.tv instead of a real ZCL frame,
    since outlet 2 has no physical endpoint a real frame could reach.
    """

    OUTLET_IDX = 1

    async def write_attributes(self, attributes, manufacturer=None):
        for attr, value in attributes.items():
            attr_id = (
                self.find_attribute(attr).id if isinstance(attr, str) else attr
            )
            _LOGGER.debug(
                "C4 Outlet2DimmerLevel: caching local attr 0x%04X = %s",
                attr_id, value,
            )
            self._update_attribute(attr_id, value)
        return [[foundation.WriteAttributesStatusRecord(ZCLStatus.SUCCESS)]]

    async def _poll_c4_outlet_state(self) -> None:
        """Send a C4 Get command to query outlet 2's current level."""
        device = self.endpoint.device
        seq = next_c4_seq(device)
        cmd = f"0g{seq:04x} c4.dm.tv {self.OUTLET_IDX:02x} 00"
        data = _build_c4_frame(0, cmd)

        _LOGGER.debug("C4 Outlet2DimmerLevel: polling — %s", cmd)
        try:
            await device.request(
                profile=C4_PROFILE_BUTTON,
                cluster=C4_CLUSTER_ID,
                src_ep=1, dst_ep=1,
                sequence=device.get_sequence(),
                data=data,
                expect_reply=False,
            )
        except Exception as exc:
            _LOGGER.warning("C4 Outlet2DimmerLevel: poll failed: %s", exc)

    async def _send_c4_outlet_level(self, level_pct: int) -> None:
        """Send c4.dm.tv 01 00 <level> with an arbitrary 0-100 level."""
        device = self.endpoint.device
        seq = next_c4_seq(device)
        cmd = f"0s{seq:04x} c4.dm.tv {self.OUTLET_IDX:02x} 00 {level_pct:02x}"
        data = _build_c4_frame(0, cmd)

        _LOGGER.debug("C4 Outlet2DimmerLevel: sending %s", cmd)
        try:
            await device.request(
                profile=C4_PROFILE_BUTTON,
                cluster=C4_CLUSTER_ID,
                src_ep=1, dst_ep=1,
                sequence=device.get_sequence(),
                data=data,
                expect_reply=False,
            )
        except Exception as exc:
            _LOGGER.warning("C4 Outlet2DimmerLevel: send failed: %s", exc)

    async def command(
        self,
        command_id,
        *args,
        manufacturer=None,
        expect_reply=False,
        tsn=None,
        **kwargs,
    ):
        if command_id in (
            LevelControl.ServerCommandDefs.move_to_level.id,
            LevelControl.ServerCommandDefs.move_to_level_with_on_off.id,
        ):
            level_zcl = args[0] if args else 0
            level_pct = _zcl_level_to_c4_pct(level_zcl)
            _LOGGER.debug(
                "C4 Outlet2DimmerLevel: move_to_level zcl=%d -> c4_pct=%d",
                level_zcl, level_pct,
            )
            await self._send_c4_outlet_level(level_pct)

            # Optimistic update — device will confirm via c4.dm.tc announce
            self._update_attribute(
                LevelControl.AttributeDefs.current_level.id, level_zcl
            )
            onoff = self.endpoint.in_clusters.get(OnOff.cluster_id)
            if onoff is not None:
                onoff.update_attribute(
                    OnOff.AttributeDefs.on_off.id, level_zcl > 0
                )
            return self._SUCCESS

        # No c4.dm.tv equivalent for move/step/stop — acknowledge and drop
        # rather than forwarding to super().command(), which would send a
        # real ZCL frame this synthetic endpoint has nowhere to deliver.
        _LOGGER.debug(
            "C4 Outlet2DimmerLevel: unhandled cmd=%s, ignoring", command_id
        )
        return self._SUCCESS

    async def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None,
    ):
        """Poll outlet 2's level using C4 protocol instead of ZCL Read Attributes."""
        if not only_cache:
            await self._poll_c4_outlet_state()
        return await super().read_attributes(
            attributes, allow_cache=True, only_cache=True, manufacturer=manufacturer,
        )


# ---------------------------------------------------------------------------
# State announcements — outlet-index-1 announcements carry outlet 2's
# graduated level; everything else (including outlet index 0) defers to the
# base switch-only on/off sync, since outlet 1's current_level is owned by
# the real-ZCL / EP2-EP196 path instead.
# ---------------------------------------------------------------------------

class C4DualOutletDimmerButtonCluster(C4DualOutletButtonCluster):
    """Button/state cluster for the LOZ-5D1-W — see module docstring."""

    name         = "Control4 Dual Outlet Dimmer Events"
    ep_attribute = "c4_dual_outlet_dimmer_buttons"

    def _handle_state_announcement(self, namespace, data):
        if namespace == "c4.dm.tc" and len(data) >= 2:
            try:
                outlet_idx = int(data[0], 16)
            except (ValueError, TypeError):
                outlet_idx = None

            if outlet_idx == 1:
                try:
                    level_pct = int(data[1], 16)
                    _LOGGER.debug(
                        "C4 dual outlet dimmer: c4.dm.tc outlet=1 level=%d",
                        level_pct,
                    )
                    self._sync_level_for_outlet_2(level_pct)
                except (ValueError, TypeError) as e:
                    _LOGGER.warning(
                        "C4 dual outlet dimmer: failed to parse c4.dm.tc: "
                        "data=%s (%s)", data, e,
                    )
                return

        # Outlet index 0 (owned by real ZCL instead) and anything else fall
        # through to the base on/off-only sync.
        super()._handle_state_announcement(namespace, data)

    def _sync_level_for_outlet_2(self, level_pct):
        ep_id = OUTLET_EP_MAP.get(1)
        if ep_id is None:
            return
        try:
            ep = self.endpoint.device.endpoints.get(ep_id)
            if ep is None:
                return
            level_zcl = _c4_pct_to_zcl_level(level_pct)
            level_cluster = ep.in_clusters.get(LevelControl.cluster_id)
            onoff_cluster = ep.in_clusters.get(OnOff.cluster_id)
            if level_cluster is not None:
                _LOGGER.debug(
                    "C4 dual outlet dimmer: outlet 2 level=%d (pct=%d)",
                    level_zcl, level_pct,
                )
                level_cluster.update_attribute(
                    LevelControl.AttributeDefs.current_level.id, level_zcl
                )
            if onoff_cluster is not None:
                onoff_cluster.update_attribute(
                    OnOff.AttributeDefs.on_off.id, level_pct > 0
                )
        except Exception:
            _LOGGER.warning(
                "C4 dual outlet dimmer: outlet 2 level sync failed",
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Device quirk
# ---------------------------------------------------------------------------

class Control4LOZ5D1WDimmer(CustomDevice):
    """Control4 LOZ-5D1-W Dimming Outlet — both outlets dimmable.

    Outlet 1 (EP1) uses real ZCL Level Control (confirmed working). Outlet 2
    (synthetic EP11) uses a graduated c4.dm.tv level (unverified — see
    module docstring for the reasoning and what to check when testing).
    """

    signature = {
        "manufacturer_code": 0x1040,
        MODELS_INFO: [
            ("Control4", "LOZ-5D1-W"),
            ("Control4", "loz-5d1-w"),
            (None, "LOZ-5D1-W"),
            (None, "loz-5d1-w"),
            ("Control4", None),
        ],
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0101,
                INPUT_CLUSTERS: [
                    Basic.cluster_id,
                    Identify.cluster_id,
                    Groups.cluster_id,
                    Scenes.cluster_id,
                    OnOff.cluster_id,
                    LevelControl.cluster_id,
                    Time.cluster_id,
                ],
                OUTPUT_CLUSTERS: [],
            },
            2: {
                PROFILE_ID: C4_PROFILE_NETWORK,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
            196: {
                PROFILE_ID: C4_PROFILE_NETWORK,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID: C4_PROFILE_BUTTON,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
            # EP 198 is the key discriminator on the LOZ-5S1-W — assumed
            # present here too (still unverified for this model).
            198: {
                PROFILE_ID: C4_PROFILE_OUTLET,
                DEVICE_TYPE: 0x0101,
                INPUT_CLUSTERS:  [Basic.cluster_id],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    replacement = {
        SKIP_CONFIGURATION: True,
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0101,   # Dimmable light — real ZCL passthrough
                INPUT_CLUSTERS: [
                    C4BasicCluster,
                    Identify.cluster_id,
                    Groups.cluster_id,
                    Scenes.cluster_id,
                    C4DimmerOnOff,
                    C4DimmerLevelControl,
                    C4DimmerManufCluster,
                ],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            11: {                       # synthetic EP for outlet 2
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0101,   # Dimmable light — graduated c4.dm.tv
                INPUT_CLUSTERS: [
                    C4DimmerOnOff,
                    C4Outlet2DimmerLevelControl,
                ],
                OUTPUT_CLUSTERS: [],
            },
            2: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4ConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            196: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4ConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4DualOutletDimmerButtonCluster],
                OUTPUT_CLUSTERS: [],
            },
            198: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4OutletStateCluster],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    device_automation_triggers = {
        ("click",   "outlet_1"): {COMMAND: "click",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        ("press",   "outlet_1"): {COMMAND: "press",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        ("release", "outlet_1"): {COMMAND: "release", CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        ("click",   "outlet_2"): {COMMAND: "click",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        ("press",   "outlet_2"): {COMMAND: "press",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        ("release", "outlet_2"): {COMMAND: "release", CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
    }


# ---------------------------------------------------------------------------
# Self-register with the get_device patch
# ---------------------------------------------------------------------------
for _c4_alias in (
    "loz-5d1-w", "LOZ-5D1-W", "C4-loz-5d1-w", "C4-LOZ-5D1-W",
):
    _C4_MODEL_QUIRK_MAP[_c4_alias] = Control4LOZ5D1WDimmer
_LOGGER.warning(
    "C4 LOZ-5D1-W: registered dimmer aliases "
    "(outlet 1 real ZCL, outlet 2 graduated c4.dm.tv)"
)
_LOGGER.info("C4 LOZ-5D1-W: registered loz-5d1-w in _C4_MODEL_QUIRK_MAP")
