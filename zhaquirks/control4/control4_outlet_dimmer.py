"""ZHA quirk for the Control4 LOZ-5D1-W Dimming Outlet.

Hardware: dimmer variant of the LOZ-5S1-W dual switched outlet chassis
("D" for dimmer vs "S" for switch — see control4_outlet.py). Assumed to be
the same enclosure/firmware family based on manufacturer-code and protocol
similarity; no dedicated Wireshark capture or HA diagnostics download from
a real LOZ-5D1-W exists yet (see TODO below).

Zigbee endpoints (ASSUMED — mirrors the confirmed LOZ-5S1-W layout):
  1   — ZHA profile, device_type 0x0101, clusters [Basic … OnOff Level Time]
  2   — C4_PROFILE_NETWORK 0xC25D, cluster 0x0001 (injected by patch)
  196 — C4_PROFILE_NETWORK 0xC25D, cluster 0x0001 (injected by patch)
  197 — C4_PROFILE_BUTTON  0xC25C, cluster 0x0001 (injected by patch)
  198 — C4_PROFILE_OUTLET  0xC25E, cluster 0x0000 (auto-discovered)

Protocol — CONFIRMED portion, reused as-is from the LOZ-5S1-W captures
(same proprietary text-over-APS channel, see control4_outlet.py):
  SET command (coordinator → device, C4_PROFILE_BUTTON EP 1→1):
    0s<seq4> c4.dm.tv <outlet> 00 <level>\r\n
    outlet: 00 = outlet index 0, 01 = outlet index 1

  RESPONSE (device → coordinator, EP 197):
    0r<seq4> 000

  STATE ANNOUNCE (device → coordinator, EP 197):
    0t<seq4> sa c4.dm.tc <outlet> <level>\r\n

Protocol — UNVERIFIED assumptions specific to this file (the LOZ-5S1-W
capture only ever showed <level> as 0x00 or 0x64):
  • <level> is assumed to be a 0-100 (0x00-0x64) percentage, so values in
    between 0x00 and 0x64 should produce intermediate brightness on real
    dimmer hardware. NOT confirmed against a real LOZ-5D1-W.
  • EP2/EP196 attr 0x0000 is assumed to still be the simple on/off flag
    seen on the LOZ-5S1-W (C4OutletConfigCluster), not the raw 0-255 level
    reported by the C4-APD120 (C4ConfigCluster). The real dim level is
    instead taken from the c4.dm.tc announcement on EP197, which already
    carries a numeric level.
  • Whether outlet 1 (synthetic EP11) is independently dimmable, or only
    outlet 0 is, is unconfirmed — both are wired up here for parity with
    the switch variant.
  • Model string casing: the confirmed LOZ-5S1-W reports lowercase
    "loz-5s1-w". The original alias added for this device in
    control4_outlet.py only registered the uppercase "LOZ-5D1-W" /
    "C4-LOZ-5D1-W" forms, which would silently never match if this device
    follows the same lowercase convention as its sibling. Both casings are
    registered below defensively.

TODO before relying on this in production: pair a real LOZ-5D1-W, download
HA diagnostics (three dots on the device page → "Download diagnostics"),
and capture a Wireshark trace while dragging the HA brightness slider
through several intermediate values to confirm the level scale, the EP198
discriminator, and whether both outlets dim independently. Update this
docstring with the confirmed findings once available.
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
    _build_c4_frame,
    next_c4_seq,
    C4DimmerManufCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import C4DualOutletButtonCluster
from c4_hooks import _C4_MODEL_QUIRK_MAP

# Reused as-is: OnOff→LevelControl redirection (C4-APD120 protocol) and the
# LevelControl local-attribute caching it provides.
from control4_dimmer import C4DimmerOnOff, C4DimmerLevelControl
# Reused as-is: EP2/196 on-off-flag config cluster and EP198 state cluster,
# both confirmed on the LOZ-5S1-W sibling.
from control4_outlet import C4OutletConfigCluster, C4OutletStateCluster

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Level scaling — ZCL Level Control (0-254) <-> C4 protocol percentage (0-100)
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
# Dimmer-outlet LevelControl — speaks c4.dm.tv instead of a real ZCL Level
# Control frame. Unlike the C4-APD120 (control4_dimmer.py), this hardware
# family only responds to the text protocol confirmed in control4_outlet.py,
# so move_to_level(_with_on_off) is translated into that command instead of
# being sent over the wire as-is.
# ---------------------------------------------------------------------------

class C4OutletDimmerLevelControl(C4DimmerLevelControl):
    """LevelControl for outlet 0 (EP1) of the LOZ-5D1-W.

    Inherits C4DimmerLevelControl's local caching of on_level/transition-time
    attributes (HA writes these but this protocol has no wire equivalent for
    them) and overrides move_to_level(_with_on_off) to send c4.dm.tv instead
    of a real ZCL Level Control command.
    """

    OUTLET_IDX = 0

    async def write_attributes(self, attributes, manufacturer=None):
        # Every writable attribute on this cluster (on_level, transition
        # times, ...) is local-only — the C4 serial protocol has no ZCL
        # WriteAttributes equivalent, so nothing is ever forwarded to the
        # device (unlike C4DimmerLevelControl, which forwards non-local
        # attrs as a real ZCL write for the C4-APD120, where that works).
        for attr, value in attributes.items():
            attr_id = (
                self.find_attribute(attr).id if isinstance(attr, str) else attr
            )
            _LOGGER.debug(
                "C4 OutletDimmerLevel: caching local attr 0x%04X = %s",
                attr_id, value,
            )
            self._update_attribute(attr_id, value)
        return [[foundation.WriteAttributesStatusRecord(ZCLStatus.SUCCESS)]]

    async def _poll_c4_outlet_state(self) -> None:
        """Send a C4 Get command to query the outlet's current level."""
        device = self.endpoint.device
        seq = next_c4_seq(device)
        cmd = f"0g{seq:04x} c4.dm.tv {self.OUTLET_IDX:02x} 00"
        data = _build_c4_frame(0, cmd)

        _LOGGER.debug(
            "C4 OutletDimmerLevel: polling outlet %d — %s", self.OUTLET_IDX, cmd
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
                "C4 OutletDimmerLevel: poll outlet %d failed: %s",
                self.OUTLET_IDX, exc,
            )

    async def _send_c4_outlet_level(self, level_pct: int) -> None:
        """Send c4.dm.tv <outlet> 00 <level> with an arbitrary 0-100 level.

        Command format confirmed for 0x00/0x64 only (see control4_outlet.py);
        intermediate values are assumed but unverified on real hardware.
        """
        device = self.endpoint.device
        seq = next_c4_seq(device)
        cmd = f"0s{seq:04x} c4.dm.tv {self.OUTLET_IDX:02x} 00 {level_pct:02x}"
        data = _build_c4_frame(0, cmd)

        _LOGGER.debug("C4 OutletDimmerLevel: sending %s", cmd)
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
                "C4 OutletDimmerLevel: c4.dm.tv send failed: %s", exc
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
        if command_id in (
            LevelControl.ServerCommandDefs.move_to_level.id,
            LevelControl.ServerCommandDefs.move_to_level_with_on_off.id,
        ):
            level_zcl = args[0] if args else 0
            level_pct = _zcl_level_to_c4_pct(level_zcl)
            _LOGGER.debug(
                "C4 OutletDimmerLevel: move_to_level zcl=%d -> c4_pct=%d",
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

        # No real ZCL Level Control frame ever reaches this hardware family
        # (see module docstring) — unlike C4DimmerLevelControl's fallback,
        # which is correct for the C4-APD120, forwarding to super().command()
        # here would just send a frame the device cannot answer. move/step/
        # stop have no c4.dm.tv equivalent, so they are acknowledged and
        # dropped rather than attempted over the wire.
        _LOGGER.debug(
            "C4 OutletDimmerLevel: unhandled cmd=%s, ignoring", command_id
        )
        return self._SUCCESS

    async def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None,
    ):
        """Poll outlet level using C4 protocol instead of ZCL Read Attributes."""
        if not only_cache:
            await self._poll_c4_outlet_state()
        return await super().read_attributes(
            attributes, allow_cache=True, only_cache=True, manufacturer=manufacturer,
        )


class C4Outlet1DimmerLevelControl(C4OutletDimmerLevelControl):
    """LevelControl for outlet 1 (synthetic EP11) — same protocol, OUTLET_IDX=1."""

    OUTLET_IDX = 1


# ---------------------------------------------------------------------------
# State announcements — extends the switch-only sync with LevelControl
# ---------------------------------------------------------------------------

class C4DualOutletDimmerButtonCluster(C4DualOutletButtonCluster):
    """Button/state cluster that also syncs LevelControl.current_level.

    C4DualOutletButtonCluster (the switch-only variant in control4_outlet.py)
    collapses the c4.dm.tc <outlet> <level> announcement into a boolean
    before syncing, which is correct for a device with no dimming. Here the
    numeric level is kept so the LevelControl cluster on EP1/EP11 reflects
    the real brightness reported by the device, not just on/off.
    """

    name         = "Control4 Dual Outlet Dimmer Events"
    ep_attribute = "c4_dual_outlet_dimmer_buttons"

    def _handle_state_announcement(self, namespace, data):
        if namespace == "c4.dm.tc":
            if len(data) >= 2:
                try:
                    outlet_idx = int(data[0], 16)
                    level_pct = int(data[1], 16)
                    _LOGGER.debug(
                        "C4 dual outlet dimmer: c4.dm.tc outlet=%d level=%d",
                        outlet_idx, level_pct,
                    )
                    self._sync_level_for_outlet(outlet_idx, level_pct)
                except (ValueError, TypeError) as e:
                    _LOGGER.warning(
                        "C4 dual outlet dimmer: failed to parse c4.dm.tc: "
                        "data=%s (%s)", data, e,
                    )
            else:
                _LOGGER.warning(
                    "C4 dual outlet dimmer: c4.dm.tc too few fields: %s", data
                )
            return

        # Fall through to parent for any other namespaces (e.g. c4.dmx.*)
        super()._handle_state_announcement(namespace, data)

    def _sync_level_for_outlet(self, outlet_idx, level_pct):
        ep_id = OUTLET_EP_MAP.get(outlet_idx)
        if ep_id is None:
            _LOGGER.warning(
                "C4 dual outlet dimmer: unknown outlet index %d", outlet_idx
            )
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
                    "C4 dual outlet dimmer: ep%d (outlet %d) level=%d (pct=%d)",
                    ep_id, outlet_idx, level_zcl, level_pct,
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
                "C4 dual outlet dimmer: level sync failed", exc_info=True
            )


# ---------------------------------------------------------------------------
# Device quirk
# ---------------------------------------------------------------------------

class Control4LOZ5D1WDimmer(CustomDevice):
    """Control4 LOZ-5D1-W Dimming Outlet.

    See the module docstring: endpoint layout and the protocol level range
    are inferred from the confirmed LOZ-5S1-W and are not yet verified
    against a real LOZ-5D1-W capture.
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
            # present here too (see module docstring TODO).
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
                DEVICE_TYPE: 0x0101,   # Dimmable light — outlet 0
                INPUT_CLUSTERS: [
                    C4BasicCluster,
                    Identify.cluster_id,
                    Groups.cluster_id,
                    Scenes.cluster_id,
                    C4DimmerOnOff,
                    C4OutletDimmerLevelControl,
                    C4DimmerManufCluster,
                ],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            11: {                       # synthetic EP for outlet 2
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0101,
                INPUT_CLUSTERS: [
                    C4DimmerOnOff,
                    C4Outlet1DimmerLevelControl,
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
#
# Both casings are registered because the confirmed LOZ-5S1-W reports its
# model string in lowercase ("loz-5s1-w") but the previous provisional alias
# for this device only registered uppercase forms — see module docstring.
# ---------------------------------------------------------------------------
for _c4_alias in (
    "loz-5d1-w", "LOZ-5D1-W", "C4-loz-5d1-w", "C4-LOZ-5D1-W",
):
    _C4_MODEL_QUIRK_MAP[_c4_alias] = Control4LOZ5D1WDimmer
_LOGGER.warning("C4 LOZ-5D1-W: registered dimmer aliases")
_LOGGER.info("C4 LOZ-5D1-W: registered loz-5d1-w in _C4_MODEL_QUIRK_MAP")
