"""ZHA quirk for the Control4 LOZ-5D1-W Dimming Outlet.

CONFIRMED on real hardware: graduated dimming works on BOTH outlets,
including turning on at an arbitrary brightness and dragging the
brightness slider while a light is on (it does not revert to off — see
attempt 10 in "History" for the real bug behind that symptom).

RESOLVED (was "STILL OPEN"): the brief flash of a *different* brightness
right after turning a light back on, before it settles on the real value,
is Home Assistant's own ZHA integration doing an optimistic restore from
its `off_brightness` entity attribute — confirmed by the user directly in
Developer Tools -> States, where outlet 2's flash value (89) matched its
`off_brightness` attribute exactly. This lives in HA core/ZHA, not in
zhaquirks, runs for any dimmable ZHA light, and is *supposed* to
self-correct once the real level update arrives. It is not a bug and
nothing in this file can or should suppress it.

What WAS a real bug: outlet 1 briefly settling on a wrong FINAL value
(e.g. 188/254 ~= 73%) instead of the correct 100% after being dimmed,
turned off, then back on — see attempt 14. Attempt 13 had let outlet 1's
current_level be overwritten by every c4.dm.tc announcement the real
ramping circuit emits (including stray mid-ramp values like 0%), which
raced against the reliable real-ZCL optimistic update from both
directions: corrupting the value captured into HA's `off_brightness` at
turn-off, and corrupting the final resting value after turn-on. Attempt
14 reverted this, so outlet 1's current_level is once again driven only
by the real-ZCL optimistic update and genuine device reports. Read
"History" before changing this file again — several earlier attempts
mistook a related symptom (attempt 10) for a wire-protocol problem and
spent a full hardware-test cycle each ruling out the wrong thing.

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
  Composer253/Director/Drivers/outlet_ip_control4.c4w — which
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
  (I:/.../hc1000v2/recovery/recovery~/control4/drivers/*.c4l — ELF
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
  serialization code itself was not disassembled. Real-hardware testing
  showed the same revert-to-off symptom yet again.

  Attempt 9 (this version) stopped inferring from the driver and captured
  the real thing: the user wired the physical LOZ-5D1-W to an actual
  HC-300 Control4 controller and used Composer's own dimmer UI while
  watching the controller's driver log (a plain text log, not a packet
  capture, but it logs every outgoing/incoming Zigbee payload including
  the raw hex of the ASCII command). Decoding that hex byte-for-byte gives
  an unambiguous, ground-truth answer for SET_LEVEL:

      Executing command (SET_LEVEL) on driver Light (v2)(13)
      -> sent:      0sf082 c4.dm.tv 00 00 00
      -> confirmed: 0t6103 sa c4.dm.tc 00 00        (device echo)
      -> sent:      0sf083 c4.dm.tv 00 00 64
      -> confirmed: 0t6104 sa c4.dm.tc 00 64
      -> sent:      0sf084 c4.dm.tv 00 00 50   (0x50 = 80)
      -> confirmed: 0t6105 sa c4.dm.tc 00 50
      -> ... same pattern through 0x3c(60), 0x28(40), 0x14(20) ...
      -> identical pattern on outlet index 01 ("Light (v2) 2(15)")

  This is the exact `c4.dm.tv <outlet> 00 <level>` command attempts 1, 2,
  and 4 already tried and reported as failing on real hardware — except
  now there is proof, from the device's own confirming announcement, that
  the device correctly accepts and applies every one of these graduated
  values. The wire format was right all along. Since attempts 1/2/4 sent
  what appears to be the identical command and reported it not working in
  Home Assistant, the most likely explanation is a bug in this quirk's own
  optimistic-update or announcement-parsing logic rather than the command
  itself — this version adds substantially more debug logging around both
  (in C4Outlet2DimmerLevelControl.command()/_send_c4_outlet_level and
  C4DualOutletDimmerButtonCluster._handle_state_announcement/
  _sync_level_for_outlet_2) so that if it still misbehaves, HA's log will
  show exactly which half of the round trip is failing.

  The same controller log also gives RAMP_TO_LEVEL's real wire format —
  `0if088 c4.dm.rtl 00 32 000003e8` decodes to an "interrupt" (0i, not
  0s/0g) frame with `<outlet> <level_hex2> <time_ms_hex8>` (8 hex digits
  of milliseconds, not the 4-digit guess in attempt 8) — but this version
  does not use it: c4.dm.tv alone is sufficient and now fully confirmed,
  so real device-side ramping is left as a possible future enhancement
  rather than another source of risk.

  Attempt 10 found the actual bug, using the debug logging attempt 9 added.
  The user's own HA debug log showed, for every single dim attempt:

      C4 Outlet2DimmerLevel: move_to_level cmd=0x04 args=() zcl=0 -> c4_pct=0
      C4 Outlet2DimmerLevel: sending 0s004a c4.dm.tv 01 00 00

  `args` was an EMPTY tuple every time, regardless of the brightness
  requested, so `level_zcl = args[0] if args else 0` silently fell back to
  0 on every call — the device was always being told to turn off, which
  exactly matches every symptom reported since the very first bug report
  in this saga. This was never a wire-protocol problem: `move_to_level(_
  with_on_off)` can arrive with the level passed as a `level=` keyword
  argument instead of positionally (confirmed present in `kwargs` on the
  user's zigpy/Python 3.14 stack), and this class's command() only ever
  checked `args[0]`. C4DimmerLevelControl (outlet 1's class) never hit
  this bug because it blindly forwards `*args, **kwargs` straight into a
  real ZCL send instead of extracting the level itself. Fixed by checking
  `kwargs["level"]` when `args` is empty.

  Attempt 11 fixed a smaller, related UX gap the user found once dimming
  itself worked: dim outlet 2 to 50%, turn it off, then back on — the UI
  briefly showed the stale pre-off level (50%) before correcting itself to
  the real value (100%, since plain on/off always drives the outlet fully
  on/off) once the device's c4.dm.tc announcement arrived. Outlet 1 never
  shows this because its on/off redirects through a real ZCL frame, so
  on_off and current_level arrive together in the same round trip.
  C4Outlet1OnOff's optimistic update only ever sets on_off (it was written
  for the LOZ-5S1-W switch, which has no LevelControl cluster to keep in
  sync) — added C4Outlet2OnOff, a thin subclass used only on outlet 2,
  that also mirrors the same always-100%/0% value into current_level
  immediately after on/off/toggle. The user reports this did NOT fix the
  symptom for outlet 2 — see attempt 12's note on this being unresolved.

  Attempt 12 found the same missing sync on outlet 1: the user noticed
  that after dimming outlet 1, turning it off then back on also left the
  UI showing the stale pre-off level, even though the light itself
  correctly went to 100%. C4DimmerLevelControl (control4_dimmer.py, used
  unchanged by outlet 1) forwards move_to_level(_with_on_off) straight to
  a real ZCL send and never optimistically updates current_level itself —
  it relies entirely on the device reporting the new level back via
  EP2/EP196 (C4ConfigCluster -> _sync_ep1_level), which is either slow or
  not firing reliably enough for this device. Added
  C4DimmerLevelControlWithOptimisticSync, a subclass scoped to this file
  (control4_dimmer.py and the real C4-APD120 it serves are left
  untouched), that mirrors current_level/on_off from whatever level was
  actually requested right after sending it — checking `kwargs["level"]`
  too, the same args-vs-kwargs gap from attempt 10.

  Neither this nor attempt 11's equivalent for outlet 2 was confirmed to
  fully fix the "off then back on" symptom, and outlet 2's report
  suggested it might not be enough by itself.

  Attempt 13 used the requested fresh debug-log capture (dim, off, back
  on) and found two things. First, outlet 2's own trace was fully
  correct: `C4 Outlet2OnOff: syncing current_level=254 to match
  on_off=True` fires immediately on turning on, well before the device's
  own c4.dm.tc confirmation arrives — zigpy's cache never held a stale
  value at any point in the log. If the UI still shows a stale brightness
  despite this, the cache this quirk controls is not the cause; the next
  place to look is Home Assistant's own light-entity/frontend state
  (outside what a ZHA quirk can fix). Second, and unexpectedly: the same
  log showed outlet 0 (EP1, outlet 1) sending c4.dm.tc announcements with
  a graduated level (e.g. 97%, 34%, 0%, 3%, 98% while the user dimmed it)
  — something not previously known to happen, since outlet 1 was assumed
  to rely solely on the real-ZCL path plus EP2/EP196 for confirmation.
  C4DualOutletButtonCluster's base _sync_onoff_for_outlet was collapsing
  these to a boolean and discarding the actual level for outlet index 0.
  Generalized _sync_level_for_outlet_2 into _sync_level_for_outlet(
  outlet_idx, level_pct) so outlet 1 also gets its current_level kept in
  sync from this channel — a second, independent confirmation path
  alongside the EP2/EP196 report and the optimistic update from attempt
  12, in case either of those is unreliable for this device.

  This is a genuine improvement (outlet 1's real-time level tracking was
  incomplete before), but it targets a newly-discovered gap, not
  necessarily the exact "off then on" symptom, which outlet 2's clean
  trace suggests may live outside this file entirely.

  Attempt 14 reverted attempt 13's outlet-0 half after it caused a real
  regression: the user tested it and found outlet 1 now settling on a
  wrong value (73%) instead of turning fully on (100%) after being
  dimmed, off, then on again — worse than the pre-attempt-13 behavior. The
  real APD120-style circuit sends MULTIPLE c4.dm.tc announcements while
  ramping toward a target (confirmed in the debug log: 97%, 34%, 0%, 3%,
  98% in quick succession while the user was interacting with outlet 1),
  and syncing current_level from every single one raced against the
  reliable real-ZCL optimistic update — whichever arrived last won,
  including a transient mid-ramp value if the announcement stream didn't
  end exactly on the target or arrived out of order. _sync_level_for_
  outlet is back to outlet-1-only; outlet 0's current_level is once again
  owned exclusively by the real-ZCL path
  (C4DimmerLevelControlWithOptimisticSync) and EP2/EP196, with no
  c4.dm.tc-based override.

  The user then checked Developer Tools -> States directly (not just the
  dashboard card) and found the *entity's own* brightness going
  null (off) -> 0 -> 188 for outlet 1, versus null -> 89 -> 253 for
  outlet 2 — and noticed outlet 2's mid-value (89) exactly matched that
  entity's `off_brightness` attribute. That pinpointed the "flash" itself
  as Home Assistant's ZHA integration optimistically restoring
  `off_brightness` on turn-on, entirely outside this file (see the
  module-level "RESOLVED" note) — outlet 2's flash was always going to
  self-correct (253 ~= 100%, correct). Outlet 1's case is the attempt-13
  bug described above wearing two faces at once: the stray c4.dm.tc
  announcements corrupted current_level both at the moment it got
  captured into `off_brightness` on turn-off (landing on 0 instead of the
  real prior level) and again after the turn-on optimistic update fired
  (landing on 188/~73% instead of 254/100%). Removing outlet 1's
  c4.dm.tc-based override fixes both: `off_brightness` will capture
  whatever the optimistic path/real reports actually set, and nothing
  will overwrite the correct value after turn-on either. This is a
  postulated mechanism consistent with all evidence gathered so far, not
  independently re-confirmed on hardware yet — re-testing outlet 1's
  dim/off/on cycle is the way to check it.

Implementation:
  • Outlet 1 (EP1) reuses C4DimmerOnOff UNCHANGED from control4_dimmer.py
    for on/off, paired with C4DimmerLevelControlWithOptimisticSync (this
    file, see attempt 12) instead of the bare C4DimmerLevelControl for
    LevelControl — no text-command translation, real ZCL passthrough plus
    an optimistic current_level/on_off sync. EP2/EP196 reuse the base
    C4ConfigCluster (not C4OutletConfigCluster), matching the APD120's raw
    0-255 dim-level report path (_sync_ep1_level) instead of the outlet's
    on/off-flag interpretation.
  • Outlet 2 (synthetic EP11) uses C4Outlet2OnOff for on/off — a thin
    subclass of C4Outlet1OnOff (control4_outlet.py's confirmed direct
    c4.dm.tv boolean transport — no LevelControl redirect, unlike outlet
    1: that redirect exists because the real APD120 ignores standard
    On/Off, and there's no evidence this text-protocol outlet does) that
    additionally syncs current_level so it doesn't lag behind on_off (see
    attempt 11) — paired with C4Outlet2DimmerLevelControl (this file) for
    brightness, which sends the confirmed `c4.dm.tv <01> 00 <level>`
    command (same shape as on/off, just with a graduated value).
  • EP197's button/state cluster syncs current_level/on_off from
    c4.dm.tc announcements for outlet 2 only (its only level-sync path,
    since it has no real Zigbee endpoint). Outlet 1's announcements are
    left to the base class's on/off-only handling — see attempt 14: the
    real circuit emits multiple graduated announcements while ramping,
    which raced against and corrupted outlet 1's more reliable real-ZCL
    optimistic update when both fed the same current_level.
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

# C4DimmerOnOff reused UNCHANGED for outlet 1: real-ZCL transport, same as
# the C4-APD120 (see module docstring). C4DimmerLevelControl is the base
# class for both C4DimmerLevelControlWithOptimisticSync (outlet 1) and
# C4Outlet2DimmerLevelControl (outlet 2) below (local attribute-caching
# reuse only, in both cases).
from control4_dimmer import C4DimmerOnOff, C4DimmerLevelControl
# Reused (as the base of C4Outlet2OnOff) for outlet 2's on/off: the
# confirmed direct c4.dm.tv boolean transport, independent of LevelControl
# — see module docstring for why outlet 2 does NOT use the OnOff->
# LevelControl redirect that outlet 1 needs (that redirect exists because
# the real APD120 ignores standard On/Off; there's no evidence this
# text-protocol outlet does).
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
# Outlet 1 LevelControl — real ZCL passthrough plus an optimistic
# current_level/on_off sync C4DimmerLevelControl doesn't do on its own.
# ---------------------------------------------------------------------------

class C4DimmerLevelControlWithOptimisticSync(C4DimmerLevelControl):
    """C4DimmerLevelControl, but also optimistically updates current_level.

    CONFIRMED bug found on real hardware: after dimming outlet 1, turning
    it off then back on made the UI keep showing the *stale pre-off level*
    even though the physical light correctly went to 100%. C4DimmerOnOff's
    on()/off() handlers (control4_dimmer.py) redirect into a real ZCL
    move_to_level_with_on_off frame via C4DimmerLevelControl, which just
    forwards it to a real wire send — it has no optimistic update of its
    own, relying entirely on the device reporting current_level back
    (EP2/EP196's C4ConfigCluster -> _sync_ep1_level). That confirmation
    apparently isn't arriving reliably enough for this specific device to
    keep the UI in sync, unlike the real C4-APD120 this class was written
    for — hence a subclass scoped to this file instead of a change to
    control4_dimmer.py itself, which is left untouched.

    This also fixes the same args-vs-kwargs gap found in outlet 2's
    C4Outlet2DimmerLevelControl (see module docstring, attempt 10):
    move_to_level(_with_on_off) can deliver the level as a `level=`
    keyword instead of positionally, so both are checked here too, even
    though C4DimmerOnOff itself always calls with a positional level.
    """

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
            if args:
                level_zcl = args[0]
            elif "level" in kwargs:
                level_zcl = kwargs["level"]
            else:
                level_zcl = None
            if level_zcl is not None:
                _LOGGER.debug(
                    "C4 DimmerLevel outlet1: optimistic current_level=%d "
                    "on_off=%s", level_zcl, level_zcl > 0,
                )
                self._update_attribute(
                    LevelControl.AttributeDefs.current_level.id, level_zcl
                )
                onoff = self.endpoint.in_clusters.get(OnOff.cluster_id)
                if onoff is not None:
                    onoff.update_attribute(
                        OnOff.AttributeDefs.on_off.id, level_zcl > 0
                    )
        return result


# ---------------------------------------------------------------------------
# Outlet 2 OnOff — C4Outlet1OnOff plus a current_level sync the switch-only
# base class has no reason to know about.
# ---------------------------------------------------------------------------

class C4Outlet2OnOff(C4Outlet1OnOff):
    """C4Outlet1OnOff, but also optimistically syncs current_level.

    CONFIRMED bug found on real hardware: after dimming outlet 2 to a
    graduated level (e.g. 50%), turning it off then back on made the UI
    briefly show the *stale pre-off level* (50%) before correcting itself
    to the real value (100%) a couple hundred ms later, once the device's
    own c4.dm.tc announcement arrived. Outlet 1 never shows this, because
    its on/off redirects through a real ZCL Level Control frame
    (C4DimmerOnOff / C4DimmerLevelControl), so on_off and current_level
    arrive and update together in the same round trip.

    C4Outlet1OnOff (control4_outlet.py) doesn't have this problem itself —
    on the LOZ-5S1-W switch it's built for, there is no LevelControl
    cluster on the endpoint to be stale in the first place. Outlet 2 does
    have one (C4Outlet2DimmerLevelControl below), so its on/off needs to
    keep current_level in sync too. The plain on/off command is confirmed
    to always drive the outlet fully on (0x64) or off (0x00) — see
    control4_outlet.py's C4OutletOnOff._send_c4_outlet_command — so this
    mirrors that same 100%/0% value into current_level immediately,
    instead of leaving it at whatever level was last set before the light
    was turned off.
    """

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
            OnOff.ServerCommandDefs.on.id,
            OnOff.ServerCommandDefs.off.id,
            OnOff.ServerCommandDefs.toggle.id,
        ):
            # super() already sent the c4.dm.tv 01 00 64/00 command and
            # optimistically set on_off to its final value (toggle is
            # resolved to on/off internally before that happens) — mirror
            # the same always-100%/0% value into current_level so the two
            # attributes agree immediately instead of only after the real
            # c4.dm.tc announcement arrives.
            is_on = self.get("on_off")
            level_cluster = self.endpoint.in_clusters.get(LevelControl.cluster_id)
            if level_cluster is not None:
                level_zcl = _c4_pct_to_zcl_level(100 if is_on else 0)
                _LOGGER.debug(
                    "C4 Outlet2OnOff: syncing current_level=%d to match "
                    "on_off=%s", level_zcl, is_on,
                )
                level_cluster.update_attribute(
                    LevelControl.AttributeDefs.current_level.id, level_zcl
                )
        return result


# ---------------------------------------------------------------------------
# Outlet 2 LevelControl — no real endpoint, so it speaks c4.dm.tv with a
# graduated level instead of the real ZCL frame outlet 1 uses.
# ---------------------------------------------------------------------------

class C4Outlet2DimmerLevelControl(C4DimmerLevelControl):
    """LevelControl for outlet 2 (synthetic EP11), via the CONFIRMED SET_LEVEL command.

    CONFIRMED from a real HC-300 controller's own log (not inference this
    time): the user connected the physical device to a real Control4
    controller and captured its driver log while using Composer's own
    dimmer UI. Decoding the logged Zigbee packets byte-for-byte shows:

        Executing command (SET_LEVEL) on driver Light (v2)(13)
        -> sent:      0sf082 c4.dm.tv 00 00 00
        -> confirmed: 0t6103 sa c4.dm.tc 00 00       (device echo)
        -> sent:      0sf083 c4.dm.tv 00 00 64
        -> confirmed: 0t6104 sa c4.dm.tc 00 64
        -> sent:      0sf084 c4.dm.tv 00 00 50   (0x50 = 80)
        -> confirmed: 0t6105 sa c4.dm.tc 00 50
        -> ... same pattern down to 0x3c(60), 0x28(40), 0x14(20) ...

    and the identical pattern for the second outlet (outlet index 01,
    "Light (v2) 2(15)" in the log). This is the SAME `c4.dm.tv <outlet> 00
    <level>` command already confirmed for on/off — it simply also accepts
    values between 0x00 and 0x64, and the device announces each one back.

    BUG FOUND AND FIXED, not yet confirmed on real hardware (module
    docstring's History, attempt 10): the wire command above was always
    correct. The actual bug was in this class's own command() — it read
    the requested brightness from `args[0]`, but on the user's zigpy/
    Python 3.14 stack, move_to_level(_with_on_off) can deliver it as a
    `level=` keyword argument instead, leaving `args`
    empty and silently sending level 0 on every single dim request. Fixed
    by falling back to `kwargs["level"]` when `args` is empty.

    The same log also confirms RAMP_TO_LEVEL's real wire format —
    `0i<seq> c4.dm.rtl <outlet> <level_hex2> <time_ms_hex8>` (an
    "interrupt" frame, 8 hex digits of milliseconds, not the 4-digit
    hex/"set" frame guessed in the previous revision) — but this class
    does not use it: c4.dm.tv alone is sufficient to set any level, and
    every earlier guess at c4.dm.rtl's shape has cost a full hardware test
    cycle to disprove, so it's deliberately left unused pending a reason
    to need real device-side ramping.

    On/off itself does NOT go through this class — see C4Outlet1OnOff in
    control4_outlet.py, reused unchanged below, which keeps using the
    confirmed c4.dm.tv on/off transport with the fixed 0x64/0x00 values.

    Inherits C4DimmerLevelControl's local caching of on_level/transition-time
    attributes but overrides write_attributes (never forward to the device —
    this protocol has no ZCL WriteAttributes equivalent at all) and
    move_to_level(_with_on_off) to send c4.dm.tv instead of a real ZCL
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
        """Send c4.dm.tv <outlet> 00 <level> — confirmed from a real
        controller's log, see class docstring.
        """
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
            _LOGGER.debug(
                "C4 Outlet2DimmerLevel: device.request() for %s completed "
                "without raising", cmd,
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
            # Confirmed via a real HA/zigpy debug log: this command can
            # arrive with an EMPTY args tuple, with "level" passed as a
            # keyword argument instead (schema-field-name calling
            # convention). args[0] alone silently defaulted to 0 every
            # time, which is why every dim attempt turned the light off
            # regardless of the requested brightness — see module
            # docstring's History, attempt 10.
            if args:
                level_zcl = args[0]
            elif "level" in kwargs:
                level_zcl = kwargs["level"]
            else:
                level_zcl = 0
            level_pct = _zcl_level_to_c4_pct(level_zcl)
            _LOGGER.debug(
                "C4 Outlet2DimmerLevel: move_to_level cmd=0x%02x args=%s "
                "kwargs=%s zcl=%d -> c4_pct=%d",
                command_id, args, kwargs, level_zcl, level_pct,
            )
            await self._send_c4_outlet_level(level_pct)

            # Optimistic update — device will confirm via a c4.dm.tc announce
            _LOGGER.debug(
                "C4 Outlet2DimmerLevel: optimistic update current_level=%d "
                "on_off=%s", level_zcl, level_zcl > 0,
            )
            self._update_attribute(
                LevelControl.AttributeDefs.current_level.id, level_zcl
            )
            onoff = self.endpoint.in_clusters.get(OnOff.cluster_id)
            if onoff is not None:
                onoff.update_attribute(
                    OnOff.AttributeDefs.on_off.id, level_zcl > 0
                )
            else:
                _LOGGER.warning(
                    "C4 Outlet2DimmerLevel: no OnOff cluster found on "
                    "endpoint %s to optimistically update",
                    self.endpoint.endpoint_id,
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
        _LOGGER.debug(
            "C4 dual outlet dimmer: announcement namespace=%r data=%s",
            namespace, data,
        )
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
                    self._sync_level_for_outlet(1, level_pct)
                except (ValueError, TypeError) as e:
                    _LOGGER.warning(
                        "C4 dual outlet dimmer: failed to parse c4.dm.tc: "
                        "data=%s (%s)", data, e,
                    )
                return

        # Outlet index 0 (owned by real ZCL instead) and anything else fall
        # through to the base on/off-only sync — see REVERTED note below.
        super()._handle_state_announcement(namespace, data)

    def _sync_level_for_outlet(self, outlet_idx, level_pct):
        """Sync current_level/on_off for one outlet from a c4.dm.tc announce.

        Outlet 1 (EP11) ONLY — this is its sole confirmation path, and has
        shown no issues. A prior revision of this method also handled
        outlet 0 (EP1), on the theory that it needed a second confirmation
        path alongside real-ZCL (C4DimmerLevelControlWithOptimisticSync)
        and EP2/EP196. REVERTED: real-hardware testing showed the real
        APD120-style circuit sends MULTIPLE c4.dm.tc announcements while
        ramping (e.g. 3%, 34%, 73%, 97%... while transitioning toward a
        target), and syncing every one of them raced against the reliable
        real-ZCL optimistic update — outlet 1 started settling on a
        transient mid-ramp value (e.g. 73%) instead of the correct target
        (100%) whenever the announcement stream didn't end exactly on the
        target or arrived out of order. Outlet 0 is intentionally back to
        on/off-only syncing (via the base class's _sync_onoff_for_outlet)
        so its current_level is owned exclusively by the real-ZCL path.
        """
        ep_id = OUTLET_EP_MAP.get(outlet_idx)
        if ep_id is None:
            _LOGGER.warning(
                "C4 dual outlet dimmer: unknown outlet index %d in "
                "c4.dm.tc — cannot sync level", outlet_idx,
            )
            return
        try:
            ep = self.endpoint.device.endpoints.get(ep_id)
            if ep is None:
                _LOGGER.warning(
                    "C4 dual outlet dimmer: endpoint %s not found on "
                    "device — cannot sync outlet %d level (pct=%d)",
                    ep_id, outlet_idx, level_pct,
                )
                return
            level_zcl = _c4_pct_to_zcl_level(level_pct)
            level_cluster = ep.in_clusters.get(LevelControl.cluster_id)
            onoff_cluster = ep.in_clusters.get(OnOff.cluster_id)
            if level_cluster is not None:
                _LOGGER.debug(
                    "C4 dual outlet dimmer: outlet %d (ep %s) level=%d (pct=%d)",
                    outlet_idx, ep_id, level_zcl, level_pct,
                )
                level_cluster.update_attribute(
                    LevelControl.AttributeDefs.current_level.id, level_zcl
                )
            else:
                _LOGGER.warning(
                    "C4 dual outlet dimmer: no LevelControl cluster on "
                    "ep %s to sync level (outlet %d, pct=%d)",
                    ep_id, outlet_idx, level_pct,
                )
            if onoff_cluster is not None:
                onoff_cluster.update_attribute(
                    OnOff.AttributeDefs.on_off.id, level_pct > 0
                )
            else:
                _LOGGER.warning(
                    "C4 dual outlet dimmer: no OnOff cluster on ep %s to "
                    "sync on/off (outlet %d, pct=%d)",
                    ep_id, outlet_idx, level_pct,
                )
        except Exception:
            _LOGGER.warning(
                "C4 dual outlet dimmer: outlet %d level sync failed",
                outlet_idx, exc_info=True,
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
                    # Adds an optimistic current_level/on_off sync on top of
                    # C4DimmerLevelControl — see its docstring.
                    C4DimmerLevelControlWithOptimisticSync,
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
                    # Adds a current_level sync on top of C4Outlet1OnOff —
                    # see C4Outlet2OnOff's docstring.
                    C4Outlet2OnOff,
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
