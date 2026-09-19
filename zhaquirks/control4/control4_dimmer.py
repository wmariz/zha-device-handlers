"""ZHA quirk for the Control4 C4-APD120 Adaptive Phase Dimmer.

History:

  Button triggers fix: device_automation_triggers advertised literal
  "click"/"press"/"release" actions, but nothing in c4_button_cluster.py's
  DIMMER_EVENT_MAP ever fired those exact strings — "click"/"release" were
  simply dead (never selectable triggers actually fire), while the button's
  real single-click/hold events (SHORT_PRESS via c4.dmx.cc=1, LONG_PRESS via
  c4.dmx.hc, LONG_RELEASE via c4.dmx.he) were firing correctly but weren't
  in this list at all, so HA's automation UI never offered them for this
  device (only DOUBLE_PRESS/TRIPLE_PRESS/QUADRUPLE_PRESS, also from
  c4.dmx.cc, were present). Separately, c4.dmx.bp — which fires immediately
  on physical press-down, before the device resolves it into a click count
  or a hold — was entirely unmapped in DIMMER_EVENT_MAP, producing a
  useless "unknown_bp" zha_event instead of a real one. Mapped "bp" to a
  new "press" action (not SHORT_PRESS, to avoid double-firing SHORT_PRESS
  once from "press" and again from the click-count resolution) and added
  it plus the three previously-missing real actions to
  device_automation_triggers.

  Plain on() restoring the wrong level, not the last dimmed one:
  CONFIRMED via a real HA debug log capture on an LDZ-101. Every plain
  on() logged the exact same target level regardless of how the light
  had last been dimmed (observed: level 2, ~1%, on every single test) —
  C4DimmerOnOff's _get_on_level() was working exactly as designed, but
  current_level itself never changed: it was permanently stuck at
  whatever value the device happened to report during the initial
  pairing interview. Root cause: the device's real, live dim-level
  confirmations arrive as `c4.dm.t0c <level_hex>` (channel 0 baked into
  the verb itself, unlike the dual-outlet family's
  `c4.dm.tc <channel> <level>`, which needs an explicit channel since it
  serves two outlets) — a namespace c4_button_cluster.py's
  _handle_state_announcement had no case for, so every single one fell
  through to "unknown namespace", got logged, and was dropped. Added a
  c4.dm.t0c case (_handle_t0c_level, mirroring the existing
  c4.dmx.dim/c4.dmx.ls handlers) so current_level finally tracks the
  light's real level.

  First fix for restoring the last dimmed level on a plain on(): made
  _sync_ep1_level (c4_helpers.py, shared by c4.dmx.dim/c4.dmx.ls/c4.dm.t0c
  plus EP2/EP196's own dim-level reports) also cache the ZCL on_level
  attribute alongside current_level whenever the announced level was
  non-zero — mirroring the mechanism already proven on the LOZ-5D1-W
  outlet dimmer. CONFIRMED BROKEN on real hardware and reverted the same
  day: off() and on() both ramp (~2s / ~0.8s — see
  _get_on/off_transition), and the device emits several intermediate
  c4.dm.t0c announcements while ramping. Caching on_level from every one
  of them meant turning off captured whatever small transient value
  happened to be the last non-zero one right before hitting 0%, not the
  level the light was actually at beforehand — the user found the light
  settled dimmer and dimmer on every single off/on cycle. Reverted
  _sync_ep1_level to only touch current_level/on_off, same as before
  today.

  Final fix: cache on_level from the command actually SENT instead —
  added to C4DimmerLevelControl.command() (this file) for
  move_to_level(_with_on_off), guarded to only cache non-zero targets
  (so off()'s explicit level=0 leaves on_level untouched). This reflects
  what was actually asked for (by HA or by C4DimmerOnOff's own on()
  translation) and is immune to ramp timing entirely, since it never
  depends on any announcement arriving.
  C4DimmerLevelControlWithOptimisticSync (control4_outlet_dimmer.py,
  outlet 1) already did the equivalent of this independently in its own
  command() override, so this change is a no-op duplicate for it, not a
  behavior change — safe for both devices that share this class
  hierarchy.
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
    Groups, Identify, LevelControl, OnOff, Scenes,
)

from zhaquirks.const import (
    BUTTON,
    CLUSTER_ID,
    COMMAND,
    DEVICE_TYPE,
    DOUBLE_PRESS,
    TRIPLE_PRESS,
    QUADRUPLE_PRESS,
    ENDPOINT_ID,
    ENDPOINTS,
    INPUT_CLUSTERS,
    LONG_PRESS,
    LONG_RELEASE,
    MODELS_INFO,
    OUTPUT_CLUSTERS,
    PROFILE_ID,
    SHORT_PRESS,
    SKIP_CONFIGURATION,
)

# Ensure patches are installed before this device class is used
import c4_hooks

import c4_helpers as C4
from c4_helpers import (
    C4_BUTTON_CLUSTER_ID,
    C4_DEFAULT_ON_LEVEL,
    C4_MANUF_CLUSTER,
    C4_OFF_TRANSITION,
    C4_ON_TRANSITION,
    C4_PROFILE_BUTTON,
    C4_PROFILE_NETWORK,
    DIMMER_BUTTON_MAP,
    C4DimmerManufCluster,
    C4ConfigCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import C4ButtonCluster
from c4_led_cluster import C4LEDCluster
from c4_ramp_cluster import C4RampCluster, C4_RAMP_CLUSTER_ID
from c4_hooks import _C4_MODEL_QUIRK_MAP

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dimmer-specific clusters
# ---------------------------------------------------------------------------

class C4DimmerOnOff(CustomCluster, OnOff):
    """OnOff cluster that redirects on/off to LevelControl.

    C4 dimmers ignore standard On/Off (cluster 0x0006) commands but respond
    to move_to_level_with_on_off (cluster 0x0008, cmd 0x04).
    """

    cluster_id = OnOff.cluster_id
    _SUCCESS   = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    def _get_ramp_cluster(self):
        """Find the C4RampCluster on EP 4, if available."""
        try:
            ep4 = self.endpoint.device.endpoints.get(4)
            if ep4 is not None:
                return ep4.in_clusters.get(C4_RAMP_CLUSTER_ID)
        except Exception:
            pass
        return None

    def _get_on_transition(self) -> int:
        """Return the on-ramp time in ZCL 1/10-second units."""
        ramp = self._get_ramp_cluster()
        if ramp is not None:
            return ramp.get_on_ramp_tenths()
        return C4_ON_TRANSITION

    def _get_off_transition(self) -> int:
        """Return the off-ramp time in ZCL 1/10-second units."""
        ramp = self._get_ramp_cluster()
        if ramp is not None:
            return ramp.get_off_ramp_tenths()
        return C4_OFF_TRANSITION

    async def command(
        self,
        command_id,
        *args,
        manufacturer=None,
        expect_reply=True,
        tsn=None,
        **kwargs,
    ):
        level_cluster = self.endpoint.level

        if command_id == OnOff.ServerCommandDefs.on.id:
            level = self._get_on_level()
            on_transition = self._get_on_transition()
            _LOGGER.debug(
                "C4 OnOff: on() → move_to_level_with_on_off(%d, %d)",
                level, on_transition,
            )
            result = await level_cluster.command(
                LevelControl.ServerCommandDefs.move_to_level_with_on_off.id,
                level, on_transition,
                expect_reply=False, manufacturer=manufacturer, tsn=tsn,
            )
            return result if result is not None else self._SUCCESS

        if command_id == OnOff.ServerCommandDefs.off.id:
            off_transition = self._get_off_transition()
            _LOGGER.debug(
                "C4 OnOff: off() → move_to_level_with_on_off(0, %d)",
                off_transition,
            )
            result = await level_cluster.command(
                LevelControl.ServerCommandDefs.move_to_level_with_on_off.id,
                0, off_transition,
                expect_reply=False, manufacturer=manufacturer, tsn=tsn,
            )
            return result if result is not None else self._SUCCESS

        if command_id == OnOff.ServerCommandDefs.toggle.id:
            cached = self.get("on_off")
            if cached:
                return await self.command(
                    OnOff.ServerCommandDefs.off.id,
                    manufacturer=manufacturer, expect_reply=expect_reply,
                    tsn=tsn, **kwargs,
                )
            else:
                return await self.command(
                    OnOff.ServerCommandDefs.on.id,
                    manufacturer=manufacturer, expect_reply=expect_reply,
                    tsn=tsn, **kwargs,
                )

        return await super().command(
            command_id, *args,
            manufacturer=manufacturer, expect_reply=expect_reply,
            tsn=tsn, **kwargs,
        )

    def _get_on_level(self) -> int:
        level_cluster = self.endpoint.level
        if level_cluster is not None:
            on_level = level_cluster.get("on_level")
            if on_level is not None and 0 < on_level < 255:
                return on_level
            cached = level_cluster.get("current_level")
            if cached is not None and cached > 0:
                return cached
        return C4_DEFAULT_ON_LEVEL


class C4DimmerLevelControl(CustomCluster, LevelControl):
    """LevelControl that defaults expect_reply=False and caches local attrs."""

    cluster_id = LevelControl.cluster_id
    _SUCCESS   = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    _LOCAL_ATTRS = {
        LevelControl.AttributeDefs.on_level.id,
        LevelControl.AttributeDefs.on_off_transition_time.id,
        LevelControl.AttributeDefs.on_transition_time.id,
        LevelControl.AttributeDefs.off_transition_time.id,
        LevelControl.AttributeDefs.default_move_rate.id,
    }

    async def write_attributes(self, attributes, manufacturer=None):
        local_attrs  = {}
        device_attrs = {}

        for attr, value in attributes.items():
            attr_id = (
                self.find_attribute(attr).id
                if isinstance(attr, str) else attr
            )
            if attr_id in self._LOCAL_ATTRS:
                local_attrs[attr_id] = value
            else:
                device_attrs[attr] = value

        for attr_id, value in local_attrs.items():
            _LOGGER.debug(
                "C4 Level: caching local attr 0x%04X = %s", attr_id, value
            )
            self._update_attribute(attr_id, value)

        if device_attrs:
            return await super().write_attributes(
                device_attrs, manufacturer=manufacturer
            )
        return [[foundation.WriteAttributesStatusRecord(ZCLStatus.SUCCESS)]]

    async def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None,
    ):
        local_reads  = {}
        device_reads = []

        for attr in attributes:
            attr_id = (
                self.find_attribute(attr).id
                if isinstance(attr, str) else attr
            )
            if attr_id in self._LOCAL_ATTRS:
                local_reads[attr_id] = self.get(attr_id)
            else:
                device_reads.append(attr)

        success, failure = {}, {}
        if device_reads:
            success, failure = await super().read_attributes(
                device_reads,
                allow_cache=allow_cache, only_cache=only_cache,
                manufacturer=manufacturer,
            )
        success.update(local_reads)
        return success, failure

    async def command(
        self,
        command_id,
        *args,
        manufacturer=None,
        expect_reply=False,
        tsn=None,
        **kwargs,
    ):
        result = await super().command(
            command_id, *args,
            manufacturer=manufacturer, expect_reply=expect_reply,
            tsn=tsn, **kwargs,
        )
        if command_id in (
            LevelControl.ServerCommandDefs.move_to_level.id,
            LevelControl.ServerCommandDefs.move_to_level_with_on_off.id,
        ):
            # Cache the requested target level as on_level, but only from
            # the command actually SENT — not from the device's own
            # c4.dm.t0c announcements (c4_helpers.py's _sync_ep1_level),
            # which fire repeatedly while ramping and previously corrupted
            # on_level with a transient mid-ramp value on every off(),
            # making the light settle dimmer on each cycle (reverted; see
            # _sync_ep1_level's docstring). The command-time target is
            # what the user/HA actually asked for and is immune to ramp
            # timing, matching the mechanism already proven on the
            # LOZ-5D1-W outlet dimmer's
            # C4DimmerLevelControlWithOptimisticSync.
            level_zcl = args[0] if args else kwargs.get("level")
            if level_zcl is not None and level_zcl > 0:
                self._update_attribute(
                    LevelControl.AttributeDefs.on_level.id, level_zcl
                )
        return result if result is not None else self._SUCCESS


# ---------------------------------------------------------------------------
# Device quirk
# ---------------------------------------------------------------------------

class Control4APD120Dimmer(CustomDevice):
    """Control4 C4-APD120 Adaptive Phase Dimmer."""

    @classmethod
    def match(cls, device):
        model = getattr(device, 'model', None)
        manuf = getattr(device, 'manufacturer', None)
        _LOGGER.debug(
            "C4 APD120.match called: model=%r manuf=%r ieee=%s",
            model, manuf, getattr(device, 'ieee', '?'),
        )
        return super().match(device)

    signature = {
        "manufacturer_code": 0x1040,
        MODELS_INFO: [
            ("Control4", "C4-APD120"),
            (None, "C4-APD120"),
            ("Control4", None),
        ],
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0101,
                INPUT_CLUSTERS:  [Identify.cluster_id, C4_MANUF_CLUSTER],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            196: {
                PROFILE_ID: C4_PROFILE_NETWORK,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4.C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID: C4_PROFILE_BUTTON,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4.C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    replacement = {
        SKIP_CONFIGURATION: True,
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0101,
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
                INPUT_CLUSTERS:  [C4ButtonCluster],
                OUTPUT_CLUSTERS: [],
            },
            3: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4LEDCluster],
                OUTPUT_CLUSTERS: [],
            },
            4: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4RampCluster],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    # CONFIRMED vs. dead: "click"/"release" never fired (nothing in
    # c4_button_cluster.py's DIMMER_EVENT_MAP produces those exact literal
    # strings), while SHORT_PRESS/LONG_PRESS/LONG_RELEASE were already being
    # fired for real (via c4.dmx.cc=1 / hc / he) but were missing from this
    # list entirely, so HA's automation UI never offered them as triggers
    # for this device. "press" (c4.dmx.bp, immediate press-down before a
    # click/hold resolves) is newly wired up in DIMMER_EVENT_MAP to match.
    device_automation_triggers = {
        (_action, _btn_name): {
            COMMAND: _action,
            CLUSTER_ID: C4_BUTTON_CLUSTER_ID,
            ENDPOINT_ID: 197,
        }
        for _btn_id, _btn_name in DIMMER_BUTTON_MAP.items()
        for _action in (
            "press",
            SHORT_PRESS, DOUBLE_PRESS, TRIPLE_PRESS, QUADRUPLE_PRESS,
            LONG_PRESS, LONG_RELEASE,
        )
    }


# ---------------------------------------------------------------------------
# Self-register with the get_device patch
# ---------------------------------------------------------------------------
_C4_MODEL_QUIRK_MAP["C4-APD120"] = Control4APD120Dimmer

# The LSZ-101 / LDZ-101 in-wall dimmers speak the same proprietary protocol as
# the APD120 but report manufacturer code 0xABCD and expose the standard
# cluster set on endpoint 1.  Dispatch is by model string, so registering the
# aliases is sufficient - no separate signature is required.
for _c4_alias in (
    "LDZ-101", "LDZ-102",
    "C4-LDZ-101", "C4-LDZ-102",
):
    _C4_MODEL_QUIRK_MAP[_c4_alias] = Control4APD120Dimmer
_LOGGER.warning("C4 APD120: registered LDZ-101 dimmer aliases")
_LOGGER.info("C4 APD120: registered C4-APD120 in _C4_MODEL_QUIRK_MAP")
