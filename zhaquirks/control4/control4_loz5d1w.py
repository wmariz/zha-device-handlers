"""ZHA quirk for the Control4 LOZ-5D1-W Dual Dimmer Outlet.

Hardware: LOZ-5D1-W is the dimming counterpart of the LOZ-5S1-W dual
switched outlet (see control4_outlet.py) — same enclosure, same endpoint
layout, same ASCII c4.dm.tv / c4.dm.tc protocol over ZigBee APS. The only
functional difference is that this variant supports graduated brightness
per outlet instead of on/off only. Until this file existed, the device was
registered as an alias of Control4LOZ5S1WOutlet (see git history of
control4_outlet.py) and only worked as a plain switch.

Zigbee endpoints after interview: identical to LOZ-5S1-W (see
control4_outlet.py docstring) — EP 198 (profile 0xC25E) is the shared
signature discriminator for the whole loz-5_1-w family; it does not by
itself distinguish the switch and dimmer variants, so dispatch between
Control4LOZ5S1WOutlet and Control4LOZ5D1WDimmer is by model string
("loz-5s1-w" vs "loz-5d1-w") instead.

Protocol — LEVEL EXTENSION (inferred, NOT yet confirmed on this hardware):
  The LOZ-5S1-W's confirmed c4.dm.tv / c4.dm.tc frames already carry a
  <level> field that happens to only take the values 0x64 (100, full on)
  and 0x00 (off) on the switch-only hardware:

    SET      0s<seq4> c4.dm.tv <outlet> 00 <level>\r\n
    ANNOUNCE 0t<seq4> sa c4.dm.tc <outlet> <level>\r\n

  This quirk assumes the LOZ-5D1-W dimmer accepts and reports the full
  0x00-0x64 (0-100%) range on that same field, the same way the C4-APD120
  dimmer's EP2/EP196 "dim level" attribute (C4_ATTR_DIM_LEVEL, handled by
  C4ConfigCluster) carries a graduated value while the outlet's override
  (C4OutletConfigCluster) treats it as a plain on/off flag. This has NOT
  been confirmed against a Wireshark capture of real LOZ-5D1-W traffic —
  if brightness control misbehaves (e.g. the device only ever reports
  0/100), capture a dimming session and adjust `_zcl_to_c4_pct` /
  `_c4_pct_to_zcl` below, or the EP2/EP196 config cluster choice.

Dimmer-semantics note:
  On/off and level-control caching here mirror control4_dimmer.py
  (C4-APD120): OnOff.on()/off() redirect to a graduated level command
  (remembering the last non-zero level for on()), and `on_level` /
  transition-time ZCL attributes are cached locally rather than sent to
  the device, since the C4 serial protocol has no equivalent of them.
  Unlike the APD120, there is no ramp/EP4 cluster for this hardware family
  — every level change is sent immediately via c4.dm.tv.
"""

import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.profiles import zha
from zigpy.quirks import CustomCluster, CustomDevice
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
    C4_DEFAULT_ON_LEVEL,
    C4_MANUF_CLUSTER,
    C4_PROFILE_BUTTON,
    C4_PROFILE_NETWORK,
    C4_PROFILE_OUTLET,
    OUTLET_EP_MAP,
    _build_c4_frame,
    next_c4_seq,
    C4DimmerManufCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import C4DualOutletButtonCluster
from control4_outlet import C4OutletConfigCluster, C4OutletStateCluster
from c4_hooks import _C4_MODEL_QUIRK_MAP

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ZCL <-> C4 protocol level conversion
# ---------------------------------------------------------------------------

def _zcl_to_c4_pct(zcl_level: int) -> int:
    """Convert a ZCL LevelControl value (0-254) to a C4 protocol percent (0-100)."""
    return round(max(0, min(254, zcl_level)) * 100 / 254)


def _c4_pct_to_zcl(pct: int) -> int:
    """Convert a C4 protocol percent (0-100) to a ZCL LevelControl value (0-254).

    Matches the rounding used for c4.dmx.dim / c4.dmx.ls on the APD120
    (see c4_button_cluster.py) so both device families report brightness
    consistently.
    """
    if pct <= 0:
        return 0
    return round(min(100, pct) * 254 / 100)


# ---------------------------------------------------------------------------
# Dimmer-outlet clusters — outlet transport (c4.dm.tv) + dimmer semantics
# ---------------------------------------------------------------------------

class C4DimmerOutletLevelControl(CustomCluster, LevelControl):
    """LevelControl for one outlet of the LOZ-5D1-W, sent via c4.dm.tv.

    Mirrors C4DimmerLevelControl (control4_dimmer.py): caches on_level /
    transition-time attributes locally (the C4 serial protocol has no
    equivalent) and defaults expect_reply=False. Unlike the APD120 — whose
    LevelControl commands travel to the device as real ZCL frames — every
    level change here is translated into a c4.dm.tv ASCII SET command,
    same as C4OutletOnOff does for plain on/off in control4_outlet.py.
    """

    cluster_id = LevelControl.cluster_id
    OUTLET_IDX = 0
    _SUCCESS   = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    _LOCAL_ATTRS = {
        LevelControl.AttributeDefs.on_level.id,
        LevelControl.AttributeDefs.on_off_transition_time.id,
        LevelControl.AttributeDefs.on_transition_time.id,
        LevelControl.AttributeDefs.off_transition_time.id,
        LevelControl.AttributeDefs.default_move_rate.id,
    }

    def _get_on_level(self) -> int:
        """Return the level to restore when turning on (last known or default)."""
        on_level = self.get("on_level")
        if on_level is not None and 0 < on_level < 255:
            return on_level
        cached = self.get("current_level")
        if cached:
            return cached
        return C4_DEFAULT_ON_LEVEL

    async def _send_c4_level(self, zcl_level: int) -> None:
        """Send c4.dm.tv <outlet> 00 <pct> to set this outlet's brightness."""
        device = self.endpoint.device
        seq = next_c4_seq(device)
        pct = _zcl_to_c4_pct(zcl_level)
        cmd = f"0s{seq:04x} c4.dm.tv {self.OUTLET_IDX:02x} 00 {pct:02x}"
        data = _build_c4_frame(0, cmd)

        _LOGGER.debug(
            "C4 DimmerOutlet: outlet %d level -> zcl=%d pct=%d (%s)",
            self.OUTLET_IDX, zcl_level, pct, cmd,
        )
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
            _LOGGER.warning(
                "C4 DimmerOutlet: outlet %d level send failed: %s",
                self.OUTLET_IDX, exc,
            )

    async def write_attributes(self, attributes, manufacturer=None):
        # Every writable attribute here (on_level, transition times, ...) is
        # local-only — the C4 serial protocol has no ZCL WriteAttributes
        # equivalent, so nothing is ever forwarded to the device.
        for attr, value in attributes.items():
            attr_id = (
                self.find_attribute(attr).id if isinstance(attr, str) else attr
            )
            _LOGGER.debug(
                "C4 DimmerOutlet: caching local attr 0x%04X = %s", attr_id, value
            )
            self._update_attribute(attr_id, value)
        return [[foundation.WriteAttributesStatusRecord(ZCLStatus.SUCCESS)]]

    async def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None,
    ):
        result = {}
        for attr in attributes:
            attr_id = (
                self.find_attribute(attr).id if isinstance(attr, str) else attr
            )
            result[attr_id] = self._attr_cache.get(attr_id)
        return result, {}

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
            zcl_level = args[0] if args else 0
            await self._send_c4_level(zcl_level)
            self._update_attribute(
                LevelControl.AttributeDefs.current_level.id, zcl_level
            )
            onoff = self.endpoint.in_clusters.get(OnOff.cluster_id)
            if onoff is not None:
                onoff._update_attribute(
                    OnOff.AttributeDefs.on_off.id, zcl_level > 0
                )
            return self._SUCCESS

        _LOGGER.debug(
            "C4 DimmerOutlet: unhandled LevelControl cmd=%s, ignoring", command_id
        )
        return self._SUCCESS


class C4DimmerOutlet1LevelControl(C4DimmerOutletLevelControl):
    """LevelControl for the second outlet (outlet 1 / EP 11)."""

    OUTLET_IDX = 1


class C4DimmerOutletOnOff(CustomCluster, OnOff):
    """OnOff cluster for one outlet of the LOZ-5D1-W — redirects to LevelControl.

    Mirrors C4DimmerOnOff (control4_dimmer.py): on() restores the last
    non-zero level (or C4_DEFAULT_ON_LEVEL), off() sends level 0. Both
    travel over the same c4.dm.tv transport C4OutletOnOff uses for the
    switch-only LOZ-5S1-W, just with a graduated level instead of the
    fixed 0x64/0x00 that hardware is limited to.
    """

    cluster_id = OnOff.cluster_id
    OUTLET_IDX = 0
    _SUCCESS   = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    def _get_level_cluster(self):
        return self.endpoint.in_clusters.get(LevelControl.cluster_id)

    async def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None,
    ):
        """Poll outlet state via C4 protocol instead of ZCL Read Attributes.

        Same rationale as C4OutletOnOff.read_attributes (control4_outlet.py):
        this hardware does not answer standard ZCL Read Attributes on this
        cluster, so HA's periodic polling must go through the c4.dm.tv/
        c4.dm.tc round trip instead. Returns cached values to the caller.
        """
        if not only_cache:
            await self._poll_c4_outlet_state()

        result = {}
        for attr in attributes:
            attr_id = (
                self.find_attribute(attr).id if isinstance(attr, str) else attr
            )
            cached = self._attr_cache.get(attr_id)
            if cached is not None:
                result[attr_id] = cached
        return result, {}

    async def _poll_c4_outlet_state(self) -> None:
        """Send a C4 Get command to query this outlet's current level/state."""
        device = self.endpoint.device
        seq = next_c4_seq(device)
        cmd = f"0g{seq:04x} c4.dm.tv {self.OUTLET_IDX:02x} 00"
        data = _build_c4_frame(0, cmd)

        _LOGGER.debug(
            "C4 DimmerOutlet OnOff: polling outlet %d - %s", self.OUTLET_IDX, cmd
        )
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
            _LOGGER.warning(
                "C4 DimmerOutlet OnOff: poll outlet %d failed: %s",
                self.OUTLET_IDX, exc,
            )

    async def command(
        self,
        command_id,
        *args,
        manufacturer=None,
        expect_reply=False,
        tsn=None,
        **kwargs,
    ):
        if command_id == OnOff.ServerCommandDefs.toggle.id:
            cached = self.get("on_off")
            command_id = (
                OnOff.ServerCommandDefs.off.id
                if cached else OnOff.ServerCommandDefs.on.id
            )

        level_cluster = self._get_level_cluster()

        if command_id == OnOff.ServerCommandDefs.on.id and level_cluster is not None:
            level = level_cluster._get_on_level()
            _LOGGER.debug("C4 DimmerOutlet OnOff: on() -> level %d", level)
            return await level_cluster.command(
                LevelControl.ServerCommandDefs.move_to_level_with_on_off.id,
                level, 0,
                expect_reply=False, manufacturer=manufacturer, tsn=tsn,
            )

        if command_id == OnOff.ServerCommandDefs.off.id and level_cluster is not None:
            _LOGGER.debug("C4 DimmerOutlet OnOff: off() -> level 0")
            return await level_cluster.command(
                LevelControl.ServerCommandDefs.move_to_level_with_on_off.id,
                0, 0,
                expect_reply=False, manufacturer=manufacturer, tsn=tsn,
            )

        _LOGGER.debug(
            "C4 DimmerOutlet OnOff: unhandled cmd=%s, passing through", command_id
        )
        result = await super().command(
            command_id, *args,
            manufacturer=manufacturer, expect_reply=expect_reply,
            tsn=tsn, **kwargs,
        )
        return result if result is not None else self._SUCCESS


class C4DimmerOutlet1OnOff(C4DimmerOutletOnOff):
    """OnOff cluster for the second outlet (outlet 1 / EP 11)."""

    OUTLET_IDX = 1


# ---------------------------------------------------------------------------
# Button/state cluster — same c4.dm.tc announce, but syncs level too
# ---------------------------------------------------------------------------

class C4DualDimmerButtonCluster(C4DualOutletButtonCluster):
    """Button/state cluster for the LOZ-5D1-W.

    Identical to C4DualOutletButtonCluster (LOZ-5S1-W) except the c4.dm.tc
    state announcement's <level> field is treated as a 0-100% brightness
    value and pushed to LevelControl.current_level, instead of being
    collapsed to on/off only.
    """

    name         = "Control4 Dual Dimmer Outlet Button Events"
    ep_attribute = "c4_dual_dimmer_buttons"

    def _sync_level_for_outlet(self, outlet_idx, zcl_level):
        ep_id = OUTLET_EP_MAP.get(outlet_idx)
        if ep_id is None:
            _LOGGER.warning("C4 dual dimmer: unknown outlet index %d", outlet_idx)
            return
        try:
            ep = self.endpoint.device.endpoints.get(ep_id)
            if ep is None:
                return
            level = ep.in_clusters.get(LevelControl.cluster_id)
            onoff = ep.in_clusters.get(OnOff.cluster_id)
            if level is not None:
                level.update_attribute(
                    LevelControl.AttributeDefs.current_level.id, zcl_level
                )
            if onoff is not None:
                onoff.update_attribute(
                    OnOff.AttributeDefs.on_off.id, zcl_level > 0
                )
        except Exception:
            _LOGGER.warning("C4 dual dimmer: level sync failed", exc_info=True)

    def _handle_state_announcement(self, namespace, data):
        """Handle c4.dm.tc state announcements from the dimmer outlet.

        Format: sa c4.dm.tc <outlet_idx_hex> <level_hex>
        outlet_idx: 00 or 01
        level: 00-64 (0-100%, graduated — see module docstring)
        """
        if namespace == "c4.dm.tc":
            if len(data) >= 2:
                try:
                    outlet_idx = int(data[0], 16)
                    pct = int(data[1], 16)
                    zcl_level = _c4_pct_to_zcl(pct)
                    _LOGGER.debug(
                        "C4 dual dimmer: c4.dm.tc outlet=%d pct=%d zcl=%d",
                        outlet_idx, pct, zcl_level,
                    )
                    self._sync_level_for_outlet(outlet_idx, zcl_level)
                except (ValueError, TypeError) as e:
                    _LOGGER.warning(
                        "C4 dual dimmer: failed to parse c4.dm.tc: data=%s (%s)",
                        data, e,
                    )
            else:
                _LOGGER.warning(
                    "C4 dual dimmer: c4.dm.tc too few fields: %s", data
                )
            return

        # Not c4.dm.tc — defer to the switch/base dispatch (c4.dmx.* etc).
        super()._handle_state_announcement(namespace, data)


# ---------------------------------------------------------------------------
# Device quirk
# ---------------------------------------------------------------------------

class Control4LOZ5D1WDimmer(CustomDevice):
    """Control4 LOZ-5D1-W Dual Dimmer Outlet.

    Same physical/Zigbee layout as the LOZ-5S1-W switched outlet (see
    control4_outlet.py) — shared discriminator (EP 198, profile 0xC25E)
    and c4.dm.tv / c4.dm.tc serial protocol. This variant additionally
    exposes graduated brightness per outlet (see module docstring for the
    protocol caveat on intermediate level values).
    """

    signature = {
        "manufacturer_code": 0x1040,
        MODELS_INFO: [
            ("Control4", "loz-5d1-w"),
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
            # EP 198 is the key discriminator — APD120 and SW120 lack it.
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
                DEVICE_TYPE: 0x0101,   # Dimmable — outlet 0
                INPUT_CLUSTERS: [
                    C4BasicCluster,
                    Identify.cluster_id,
                    Groups.cluster_id,
                    Scenes.cluster_id,
                    C4DimmerOutletOnOff,
                    C4DimmerOutletLevelControl,
                    C4DimmerManufCluster,
                ],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            11: {                       # synthetic EP for outlet 2
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0101,   # Dimmable — outlet 1
                INPUT_CLUSTERS: [
                    C4DimmerOutlet1OnOff,
                    C4DimmerOutlet1LevelControl,
                ],
                OUTPUT_CLUSTERS: [],
            },
            2: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4OutletConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            196: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4OutletConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4DualDimmerButtonCluster],
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
_C4_MODEL_QUIRK_MAP["loz-5d1-w"] = Control4LOZ5D1WDimmer
for _c4_alias in ("LOZ-5D1-W", "C4-LOZ-5D1-W"):
    _C4_MODEL_QUIRK_MAP[_c4_alias] = Control4LOZ5D1WDimmer
_LOGGER.info("C4 LOZ-5D1-W: registered loz-5d1-w in _C4_MODEL_QUIRK_MAP")
