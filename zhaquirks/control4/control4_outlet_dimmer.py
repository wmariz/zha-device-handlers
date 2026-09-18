"""ZHA quirk for the Control4 LOZ-5D1-W Dimming Outlet.

CONFIRMED on real hardware: outlet 1 (EP1) dims correctly — turning on at
an arbitrary brightness works, and dragging the brightness slider while
the light is on updates the level in place (it does not revert to off).

Outlet 2 (synthetic EP11) is STILL UNRESOLVED. The Control4 controller's
own app shows it as dimmable in hardware, but every attempt at graduated
brightness for it so far has reverted to off, same as outlet 1's original
failure. See "History" below for what's been tried and ruled out — please
read it before changing this file again, to avoid repeating a dead end.

History:

  Attempts 1 and 2 (see git history: commit 438bb9a, and the commit that
  replaced it) translated LevelControl commands for BOTH outlets into the
  outlet's c4.dm.tv text SET command (`c4.dm.tv <outlet> 00 <level>`) with
  a graduated 0-100 level, exactly like C4OutletOnOff does for on/off in
  control4_outlet.py. On real hardware this made outlet 1 revert to (or
  stay) off for any level other than 0x00/0x64, and a plain turn-on got
  stuck at C4_DEFAULT_ON_LEVEL (191 -> 75%).

  Attempt 3 fixed outlet 1 by re-reading C4DimmerOnOff's docstring in
  control4_dimmer.py ("C4 dimmers ignore standard On/Off (cluster 0x0006)
  commands but respond to move_to_level_with_on_off (cluster 0x0008, cmd
  0x04)") — the real C4-APD120 gets genuine ZCL Level Control frames, not
  a synthesized text command. The LOZ-5S1-W's own interview already
  reports a native LevelControl cluster on EP1 (see control4_outlet.py's
  docstring: "clusters [Basic … OnOff Level Time]") that the switch quirk
  never wires up, because the switch doesn't dim. Outlet 1's dimming
  circuit turned out to behave like the APD120, and real ZCL passthrough
  was confirmed working. This has NO equivalent for outlet 2: it is a
  synthetic endpoint with no real Zigbee endpoint behind it (the device's
  interview only ever shows EP1/2/196/197/198), so there is nowhere to
  send a real ZCL frame — ZCL addressing has no field for "which physical
  circuit" within one endpoint/cluster.

  Attempt 4 hypothesized that outlet 1's failure fully explained the
  original attempt 1/2 result (i.e. that c4.dm.tv itself was fine and only
  outlet 1 needed real ZCL), and restored the identical graduated
  c4.dm.tv level for outlet 2 alone. Real-hardware testing showed the
  exact same revert-to-off symptom on outlet 2, which disproves that
  theory: c4.dm.tv's `<level>` field genuinely seems to reject anything
  other than 0x00/0x64, independent of which outlet it targets.

  Attempt 5 noticed that c4_ramp_cluster.py uses the same c4.dm.tv
  namespace as `<channel> <index> <value>` for the APD120's ramp
  parameters, where each index (0x01=fast, 0x02=on-ramp, 0x03=off-ramp,
  ...) is an independently addressable parameter accepting a wide value
  range (0-65535 ms) — not a fixed boolean-like pair. By that pattern, the
  outlet SET command's fixed "00" may be an *index* (plausibly "on/off
  state") rather than a filler byte, and graduated brightness may live at
  a *different*, currently unknown index instead. This guessed index 0x01.
  Real-hardware testing showed the same revert-to-off symptom again,
  disproving this theory too: the graduated level does not live at a
  different index within c4.dm.tv either.

  Attempt 6 came from reading control4-apd120-dimmer-protocol.md and
  control4-fan-controller-sf120-protocol.md (zhaquirks/control4/
  documentation/), which revealed that c4.dm.tv (no "x") is used ONLY for
  ramp/transition-time config on real dimmers — never for a live "set to
  this level now" command, explaining why attempt 5's index guess had no
  chance regardless of which index was picked. It guessed the live "set"
  command lived in a device-specific c4.dmx.* namespace, by analogy with
  the fan controller's confirmed announce `c4.dmx.fs` / set `c4.dmx.fsc`
  pair, landing on `c4.dmx.lsc`. Real-hardware testing showed the same
  revert-to-off symptom yet again.

  Attempt 7 stopped guessing from analogy and went to the source: the user
  located the actual compiled Control4 driver on their own PC —
  Composer253\Director\Drivers\outlet_ip_control4.c4w — which
  outlet_wireless_dimmer.c4i's <control> field names as the exact driver
  for this device (outlet_wireless.c4i, the LOZ-5S1-W switch's descriptor,
  names a different one). That 1MB native binary's embedded string table
  contains ZERO c4.dmx.* strings, which retroactively confirms attempt 6's
  namespace guess could never have worked for this specific driver. It
  also contains a full command catalog:
      <name>SET_LEVEL</name>
      <description>Set Level on the NAME to INTEGER</description>
      <name>RAMP_TO_LEVEL</name>
      <description>Ramp to Level INTEGER on the NAME over TIME STRING</description>
  paired with a wire-verb table that includes (alongside on/of/tv/tc/...):
      c4.dm.rtl
  "rtl" matches "Ramp To Level" letter-for-letter — a driver-confirmed
  verb name, not an analogy from a sibling device. This attempt sent
  `c4.dm.rtl <outlet> <time_ms_hex4> <level_hex2>` (time before level, by
  analogy with c4_ramp_cluster.py's `c4.dm.tv <ch> <idx> <time_ms_hex4>`).
  Real-hardware testing showed the same revert-to-off symptom again — the
  verb was right, but this attempt's argument order was apparently wrong.

  Attempt 8 (this version) replaced analogy with the actual implementation:
  the user pulled the driver binaries straight off a real HC-1000v2
  controller's recovery partition
  (I:\...\hc1000v2\recovery\recovery~\control4\drivers\*.c4l — ELF
  binaries, the ARM/Linux Director-side runtime, as opposed to the
  Windows-side .c4w files attempt 7 used). Unlike the Windows binaries,
  outlet_ip_control4.c4l is NOT stripped: its symbol table has the literal
  C++ mangled name

      _ZN18outlet_ip_control415RampOutletLevelEN8OutletID4TypeEjj

  which demangles to

      outlet_ip_control4::RampOutletLevel(OutletID::Type, unsigned int, unsigned int)

  — outlet selector, then two plain unsigned ints. Matched against the
  command's own description order ("Ramp to Level INTEGER ... over TIME
  STRING" — level named first), this reads as RampOutletLevel(outlet,
  level, time): LEVEL BEFORE TIME, the reverse of attempt 7. This version
  sends `c4.dm.rtl <outlet> <level_hex2> <time_ms_hex4>`.

  This is a real function signature, not an analogy, but it is still
  inference from a parameter list rather than a captured wire frame — the
  serialization code itself was not disassembled. If this also fails, the
  reliable next step is a Wireshark capture of the Control4 app dimming
  this outlet, to read the true byte order off the wire instead of
  inferring it from either the driver's C++ signatures or its embedded
  strings.

Implementation:
  • Outlet 1 (EP1) reuses C4DimmerOnOff / C4DimmerLevelControl UNCHANGED
    from control4_dimmer.py — no override, no text-command translation.
    EP2/EP196 reuse the base C4ConfigCluster (not C4OutletConfigCluster),
    matching the APD120's raw 0-255 dim-level report path
    (_sync_ep1_level) instead of the outlet's on/off-flag interpretation.
  • Outlet 2 (synthetic EP11) uses C4Outlet1OnOff UNCHANGED from
    control4_outlet.py for on/off (the confirmed direct c4.dm.tv boolean
    transport — no LevelControl redirect, unlike outlet 1: that redirect
    exists because the real APD120 ignores standard On/Off, and there's no
    evidence this text-protocol outlet does), paired with
    C4Outlet2DimmerLevelControl (this file) for brightness, which sends
    the driver-confirmed `c4.dm.rtl <01> <level> <time>` command.
  • EP197's button/state cluster treats outlet-index-1 c4.dm.tc
    announcements as a graduated level for outlet 2, and defers everything
    else (including any outlet-index-0 announcements) to the base sync, so
    outlet 1's current_level stays owned exclusively by the real-ZCL /
    EP2-EP196 path above.
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
# (see module docstring). C4DimmerLevelControl is also the base class for
# C4Outlet2DimmerLevelControl below (local attribute-caching reuse only).
from control4_dimmer import C4DimmerOnOff, C4DimmerLevelControl
# Reused UNCHANGED for outlet 2's on/off: the confirmed direct c4.dm.tv
# boolean transport, independent of LevelControl — see module docstring for
# why outlet 2 does NOT use the OnOff->LevelControl redirect that outlet 1
# needs (that redirect exists because the real APD120 ignores standard
# On/Off; there's no evidence this text-protocol outlet does).
from control4_outlet import C4Outlet1OnOff, C4OutletStateCluster

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
    """LevelControl for outlet 2 (synthetic EP11), via the driver's RAMP_TO_LEVEL verb.

    The verb name c4.dm.rtl ("Ramp To Level") is driver-confirmed — see the
    module docstring's History section for how it was found and why
    earlier guesses (c4.dm.tv with a different value/index, a c4.dmx.*
    verb) are now known to be wrong for this device, not just unconfirmed.

    The argument ORDER is now also evidence-based rather than guessed by
    analogy. An unstripped ARM/Linux driver binary pulled from a real
    HC-1000v2 controller's recovery partition
    (control4\\drivers\\outlet_ip_control4.c4l — the same driver identified
    from the Windows side) still has its C++ symbol table, including:

        _ZN18outlet_ip_control415RampOutletLevelEN8OutletID4TypeEjj

    which demangles to:

        outlet_ip_control4::RampOutletLevel(OutletID::Type, unsigned int, unsigned int)

    Three parameters: the outlet selector, then two plain unsigned ints.
    Paired with the command's own description order ("Ramp to Level
    INTEGER ... over TIME STRING" — level named before time), the natural
    reading is RampOutletLevel(outlet, level, time), i.e. LEVEL BEFORE
    TIME on the wire, the opposite of this class's first version (which
    also failed identically on real hardware). This class now sends
    `c4.dm.rtl <outlet> <level_hex2> <time_ms_hex4>`.

    This is stronger evidence than any prior attempt (a real function
    signature, not an analogy to a sibling device), but it is still
    inference from a C++ parameter list, not a captured wire frame. If
    this also fails, the reliable next step is a Wireshark capture of the
    Control4 app dimming this outlet, to read the true byte order off the
    wire instead of inferring it — see the module docstring.

    The ZCL transition_time argument HA already provides (ignored by every
    earlier attempt in this file) is used for <time_ms>, converting ZCL
    1/10-s units to ms.

    On/off itself does NOT go through this class — see C4Outlet1OnOff in
    control4_outlet.py, reused unchanged below, which keeps using the
    confirmed c4.dm.tv on/off transport with the fixed 0x64/0x00 values
    (also present as dedicated c4.dm.on/c4.dm.of verbs in the driver
    binary, but c4.dm.tv is already confirmed working — no reason to
    switch it).

    Inherits C4DimmerLevelControl's local caching of on_level/transition-time
    attributes but overrides write_attributes (never forward to the device —
    this protocol has no ZCL WriteAttributes equivalent at all) and
    move_to_level(_with_on_off) to send c4.dm.rtl instead of a real ZCL
    frame, since outlet 2 has no physical endpoint a real frame could reach.
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
        cmd = f"0g{seq:04x} c4.dm.rtl {self.OUTLET_IDX:02x}"
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

    async def _send_c4_outlet_level(self, level_pct: int, time_ms: int) -> None:
        """Send c4.dm.rtl 01 <level> <time_ms> — see class docstring for the
        RampOutletLevel(OutletID::Type, uint level, uint time) evidence
        behind this argument order (level before time).
        """
        device = self.endpoint.device
        seq = next_c4_seq(device)
        time_ms = max(0, min(0xFFFF, int(time_ms)))
        cmd = (
            f"0s{seq:04x} c4.dm.rtl {self.OUTLET_IDX:02x} "
            f"{level_pct:02x} {time_ms:04x}"
        )
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
            transition_tenths = args[1] if len(args) > 1 and args[1] is not None else 0
            level_pct = _zcl_level_to_c4_pct(level_zcl)
            time_ms = int(transition_tenths) * 100
            _LOGGER.debug(
                "C4 Outlet2DimmerLevel: move_to_level zcl=%d -> c4_pct=%d, "
                "transition=%d (1/10s) -> %dms",
                level_zcl, level_pct, transition_tenths, time_ms,
            )
            await self._send_c4_outlet_level(level_pct, time_ms)

            # Optimistic update — device will confirm via a c4.dm.tc announce
            self._update_attribute(
                LevelControl.AttributeDefs.current_level.id, level_zcl
            )
            onoff = self.endpoint.in_clusters.get(OnOff.cluster_id)
            if onoff is not None:
                onoff.update_attribute(
                    OnOff.AttributeDefs.on_off.id, level_zcl > 0
                )
            return self._SUCCESS

        # No c4.dm.rtl equivalent for move/step/stop — acknowledge and drop
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
        # through to the base on/off-only sync. (An earlier revision of
        # this file also overrode _handle_light_state for c4.dmx.ls —
        # removed once the actual driver binary confirmed this device never
        # sends anything in the c4.dmx.* namespace; see module docstring.)
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
                    # Confirmed direct on/off transport (index 0x00) — does
                    # NOT redirect through LevelControl, unlike outlet 1.
                    C4Outlet1OnOff,
                    # Guessed index 0x01 for graduated brightness — see its
                    # class docstring.
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
