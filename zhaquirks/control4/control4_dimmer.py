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

  Investigated two more user reports with fresh debug logs (with the
  diagnostic logging above already in place): setting brightness to
  100% settling at 99%, and transition times "not being respected."
  Both turned out to be real hardware characteristics, not bugs:
  commanding the ZCL max (254, the only valid "100%") consistently made
  the device report back 99% via c4.dm.t0c — the numbers match the
  device computing its own percentage as level/255 rather than the
  correct level/254, an off-by-one in its own firmware that no ZCL-
  compliant command can work around (255 is not a valid level to send).
  For transitions, even an explicit transition_time=0 (instant) still
  took ~0.7s for the light to visibly reach its target, while lowering
  the level with the same transition_time=0 was near-instant (~0.1s) —
  a rise-only floor consistent with a phase dimmer's soft-start
  circuitry, which off/down transitions don't need and reasonably
  tracked whatever transition time was actually commanded (~2.0s
  commanded, ~2.0-2.5s observed). Left uninvestigated further: this
  quirk is already sending the technically-correct values in both
  cases; the device's own physical/firmware behavior is what falls a
  little short, in a way this quirk cannot fix while staying
  ZCL-compliant.

  That same debug log surfaced a related, confirmed bug: unlike
  outlet_dimmer.py's outlet 1, this file's C4DimmerLevelControl had no
  optimistic sync of its own, so current_level (and therefore what HA's
  UI displays) was driven purely by live c4.dm.t0c announcements — and
  since the device reports every intermediate value while physically
  ramping (e.g. 10%, 95%, 99% over about a second), the UI visibly
  flashed through each one instead of jumping straight to the requested
  target. The user confirmed they want this to behave like the outlet
  dimmer instead. Added the same current_level/on_off optimistic sync
  C4DimmerLevelControlWithOptimisticSync already does, directly to
  C4DimmerLevelControl.command() (this file) — current_level now jumps
  to the requested target the instant a command is sent, and later
  c4.dm.t0c announcements still correct it if the device's real settled
  level differs slightly (e.g. the 99%-not-100% case above).

  CONFIRMED bug in that fix: jumping optimistically wasn't enough by
  itself. The very next intermediate c4.dm.t0c reading (arriving well
  under a second later) immediately overwrote the optimistic value via
  _sync_ep1_level (c4_helpers.py), which unconditionally applies every
  live level_raw it receives — so the user still saw the UI flash
  through the live ramp, now with an extra flash to the target tacked
  on first. Added a short suppression window instead of just an
  optimistic write: C4DimmerLevelControl.command() now also calls
  c4_helpers.c4_suppress_level_sync() right after the optimistic update,
  and _sync_ep1_level checks it before touching current_level/on_off,
  skipping any announcement that arrives before it elapses.
  _LEVEL_SYNC_SUPPRESS_SECONDS (this file) is set to 3.0s — comfortably
  longer than either measured ramp direction (~1.3s on, ~2.5s off) —
  after which live announcements are trusted normally again, so a
  genuine physical adjustment at the wall switch (which this quirk never
  commands and so never suppresses) still reaches HA as before.

  CONFIRMED BUG in the first TWO versions of that suppression mechanism
  (both in c4_helpers.py, root-caused with an id()-logging debug
  capture — see _sync_ep1_level's own docstring for the full detail):
  storing the deadline as a plain instance attribute on the LevelControl
  cluster never read back correctly, and switching to a module-level
  dict keyed by device.ieee *looked* like the fix but failed identically
  — logging id() of the dict at both the write and read sites proved
  why: they were two different dict objects, because Home Assistant's
  custom-quirks loader evidently imports c4_helpers.py more than once.
  This module cannot be relied on as a singleton in this environment.

  Final fix: c4_suppress_level_sync() (c4_helpers.py) now stores the
  deadline directly on the zigpy `device` object itself instead of
  anywhere in c4_helpers.py's own state — `device` is passed in by the
  caller (self.endpoint.device, below) rather than looked up through a
  module, so it is genuinely one single object regardless of which
  import of c4_helpers.py happens to be running.

  Missing button-click entity: the user pointed out that after the
  earlier button-triggers fix (above), automations could still be built
  from "top pressed" etc., but there was no actual HA *entity* anywhere
  reflecting button activity — nothing in Developer Tools -> States, no
  history, nothing to put on a dashboard. zha_send_event alone only
  produces a bus event (zha_event), never an entity. Two dead ends before
  the real fix, both CONFIRMED WRONG on real hardware:
    1. A bare EventableCluster (on the unverified assumption, borrowed
       from the KC120277 scene controller's own docstring, that ZHA
       creates an Event entity for it automatically). No entity of any
       kind appeared — EventableCluster's only real behavior is firing
       the classic zha_event bus message, not creating an entity.
    2. A plain MultistateInput cluster with a custom per-button
       `ep_attribute`. Still no entity after a full reload, and no
       disabled entity either — the cluster type wasn't the (only)
       problem.
  Root cause, found in this same directory's control4_z2io_zp.py
  (C4ContactCluster, a CONFIRMED-working BinaryInput-backed binary_sensor
  on this exact device family): `ep_attribute` must stay each ZCL
  cluster's own inherited default for ZHA's ClusterHandler discovery to
  resolve it and create an entity — overriding it with a custom name (as
  both attempts above did) silently breaks discovery, regardless of
  cluster type. Final fix: _make_dimmer_button_cluster
  (c4_button_cluster.py) now builds a real BinaryInput cluster (0x000F)
  with its default ep_attribute intact; per-button uniqueness comes from
  each one living on its own virtual endpoint
  (DIMMER_BUTTON_EVENT_EP_MAP: 198 "top", 199 "bottom"), not from the
  ep_attribute. present_value goes True on press and False once the
  press resolves (a simple click, or a hold ending), so each button now
  gets a real binary_sensor entity whose on/off state reflects press and
  release. C4DimmerButtonCluster (replacing bare C4ButtonCluster on
  EP197) overrides only _fire_button_zha_event() — a new override point
  split out of C4ButtonCluster._handle_button_event() specifically for
  this — so the dimmer's own on/off state sync (_sync_state_from_event /
  _sync_cc_event, unique to this class; the scene controller has no
  load to sync) keeps running unchanged, and only *where* the
  zha_event fires (plus the new present_value updates) happens: on the
  matching virtual endpoint instead of EP197. device_automation_triggers
  was updated to point at the virtual endpoints too, since events no
  longer fire on EP197 at all once C4DimmerButtonCluster is in use.

  Button/LED-attached hardware config: CONFIRMED from a real HC300
  controller log capturing Composer's own SET_BUTTON_ATTACHED /
  SET_LED_ATTACHED on the LDZ-101 — `c4.dm.ba <0|1>` and
  `c4.dm.lm <0|1>` respectively (single decimal digit, no hex padding,
  unlike c4.dm.tv's indexed ramp values). First added as ZCL commands on
  C4RampCluster, then as custom Bool attributes there — both confirmed
  broken on real hardware for producing a usable HA control (a command
  never gets its own entity and rendered as a 0-255 slider; a bare
  attribute on a fully custom cluster gets no auto-generated entity
  either, since ZHA only has hardcoded platform support for a handful
  of core-recognized ZCL attributes). Final fix: two new virtual
  endpoints (ATTACHED_SWITCH_EP_MAP: 200 "button_attached", 201
  "led_attached" — c4_attached_switch.py), each carrying nothing but a
  plain OnOff cluster whose on/off/toggle commands send the wire
  command instead of controlling real power. ZHA's switch-platform
  discovery reliably turns any OnOff cluster into a real Switch entity,
  which is the same virtual-endpoint trick already used above for the
  two button Event entities.

  Per-button LED-color RGB lights: CONFIRMED from a real HC300 controller
  log — the top button LED's on-color is set via
  `0s<seq> c4.dm.l0o <rrggbb_hex>` (the only one of the four combos
  actually observed transmitting; l0f/l1o/l1f were requested in Composer
  but throttled before ever hitting the wire — see c4_led_rgb.py). This
  is a different namespace family from C4LEDCluster's c4.dmx.led
  (EP3, KC120277 scene-controller protocol), which is very likely
  non-functional for this device's own LEDs. Per the user's request,
  only "on" color is exposed, since the LED only follows this quirk's
  commands while led_attached is off — otherwise the device's own
  built-in logic drives it. Two new virtual endpoints
  (LED_COLOR_EP_MAP: 202 "top", 203 "bottom" — c4_led_rgb.py), each
  carrying a plain OnOff + Color cluster, so ZHA's light-platform
  discovery creates a real RGB light entity per LED (same virtual-
  endpoint trick as the button/attached-switch entities above). Color
  is set via the ZCL Hue/Saturation model rather than XY, converting to
  RGB with a plain stdlib colorsys call; turning the light off sends
  black, on re-sends the last color. Bottom LED (c4.dm.l1o) is
  UNCONFIRMED — implemented by analogy with the one command actually
  captured, pending a real-hardware test.
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
    BinaryInput, Groups, Identify, LevelControl, OnOff, Scenes,
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
    APD120_BUTTON_MAP,
    C4_DEFAULT_ON_LEVEL,
    C4_MANUF_CLUSTER,
    C4_OFF_TRANSITION,
    C4_ON_TRANSITION,
    C4_PROFILE_BUTTON,
    C4_PROFILE_NETWORK,
    DIMMER_BUTTON_EVENT_EP_MAP,
    C4DimmerManufCluster,
    C4ConfigCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import C4DimmerButtonCluster, _DIMMER_BUTTON_CLUSTERS
from c4_led_cluster import C4LEDCluster
from c4_ramp_cluster import C4RampCluster, C4_RAMP_CLUSTER_ID
from c4_attached_switch import (
    ATTACHED_SWITCH_EP_MAP,
    C4ButtonAttachedOnOff,
    C4LedAttachedOnOff,
)
from c4_led_rgb import (
    LED_COLOR_EP_MAP,
    C4LedOnOff,
    C4TopLedColorCluster,
    C4BottomLedColorCluster,
)
from c4_hooks import _C4_MODEL_QUIRK_MAP

_LOGGER = logging.getLogger(__name__)

# How long to ignore live c4.dm.t0c/c4.dmx.dim/c4.dmx.ls/EP2-EP196 level
# announcements after C4DimmerLevelControl optimistically jumps
# current_level to a just-commanded target (see its command() and
# _sync_ep1_level in c4_helpers.py). CONFIRMED on real hardware: the
# device emits several intermediate readings while physically ramping,
# taking up to ~1.3s to turn on and ~2.5s to turn off (see this module's
# History) — this window comfortably covers both directions with margin,
# so those readings don't overwrite the optimistic value and flash the
# UI through the live ramp. A real physical adjustment at the wall
# switch is still picked up normally once the window has passed.
_LEVEL_SYNC_SUPPRESS_SECONDS = 3.0


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
            tenths = ramp.get_on_ramp_tenths()
            _LOGGER.debug(
                "C4 OnOff: on-ramp from C4RampCluster cache = %d tenths "
                "(%d ms cached, NOT read from the real device — see "
                "c4_ramp_cluster.py's docstring)",
                tenths, ramp.get_ramp_ms(0x02),
            )
            return tenths
        _LOGGER.debug(
            "C4 OnOff: no C4RampCluster found on EP4 — falling back to "
            "hardcoded C4_ON_TRANSITION=%d tenths", C4_ON_TRANSITION,
        )
        return C4_ON_TRANSITION

    def _get_off_transition(self) -> int:
        """Return the off-ramp time in ZCL 1/10-second units."""
        ramp = self._get_ramp_cluster()
        if ramp is not None:
            tenths = ramp.get_off_ramp_tenths()
            _LOGGER.debug(
                "C4 OnOff: off-ramp from C4RampCluster cache = %d tenths "
                "(%d ms cached, NOT read from the real device — see "
                "c4_ramp_cluster.py's docstring)",
                tenths, ramp.get_ramp_ms(0x03),
            )
            return tenths
        _LOGGER.debug(
            "C4 OnOff: no C4RampCluster found on EP4 — falling back to "
            "hardcoded C4_OFF_TRANSITION=%d tenths", C4_OFF_TRANSITION,
        )
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
        if command_id in (
            LevelControl.ServerCommandDefs.move_to_level.id,
            LevelControl.ServerCommandDefs.move_to_level_with_on_off.id,
        ):
            # Diagnostic only (no behavior change): this is the ONLY place
            # that logs what gets sent when HA/ZHA calls LevelControl
            # directly (e.g. dragging the brightness slider, or "set to
            # X%"), bypassing C4DimmerOnOff.command() entirely — that
            # class only logs the level/transition IT computes for a
            # plain on()/off(). Added to investigate two user reports:
            # setting 100% settling at ~98%, and transition times seeming
            # not to be honored — need to see the real args/kwargs this
            # cluster actually forwards as a genuine ZCL frame to tell
            # whether the level/transition requested is already wrong
            # before it reaches the device, or whether the device itself
            # is receiving the right numbers and doing something else.
            _LOGGER.debug(
                "C4 Level: command=0x%02x args=%s kwargs=%s (forwarding "
                "as real ZCL move_to_level%s)",
                command_id, args, kwargs,
                "_with_on_off" if command_id ==
                LevelControl.ServerCommandDefs.move_to_level_with_on_off.id
                else "",
            )
        result = await super().command(
            command_id, *args,
            manufacturer=manufacturer, expect_reply=expect_reply,
            tsn=tsn, **kwargs,
        )
        if command_id in (
            LevelControl.ServerCommandDefs.move_to_level.id,
            LevelControl.ServerCommandDefs.move_to_level_with_on_off.id,
        ):
            level_zcl = args[0] if args else kwargs.get("level")
            if level_zcl is not None:
                # CONFIRMED on real hardware: without this, current_level
                # only ever changed when a genuine c4.dm.t0c announcement
                # arrived — and since the device actually reports EVERY
                # intermediate value while physically ramping (e.g. 10%,
                # 95%, 99% while turning on over ~1s), HA's UI visibly
                # flashed through each of them instead of jumping straight
                # to the target, unlike the LOZ-5D1-W outlet dimmer (whose
                # C4DimmerLevelControlWithOptimisticSync already does
                # exactly this). The user asked for consistency: jump
                # optimistically to the requested target immediately, the
                # same way the outlet dimmer does.
                #
                # CONFIRMED bug in the first version of this fix: jumping
                # optimistically wasn't enough by itself — the very next
                # intermediate c4.dm.t0c reading (arriving ~100ms later)
                # immediately overwrote it via _sync_ep1_level
                # (c4_helpers.py), so the UI flashed through the live ramp
                # anyway, right after an extra flash to the target first.
                # c4_suppress_level_sync() (c4_helpers.py) tells
                # _sync_ep1_level to ignore announcements for a few
                # seconds after this fires, so only the FINAL settled
                # reading (once the window has passed) can still correct
                # current_level — e.g. if the device's real settled level
                # ends up slightly different from what was asked (99%
                # instead of 100%, a device firmware characteristic — see
                # module docstring). See module docstring for why this
                # stores the deadline on the `device` object rather than
                # anywhere in c4_helpers.py's own state.
                _LOGGER.debug(
                    "C4 Level: optimistic current_level=%d on_off=%s "
                    "(suppressing live announcements for %.1fs)",
                    level_zcl, level_zcl > 0, _LEVEL_SYNC_SUPPRESS_SECONDS,
                )
                self._update_attribute(
                    LevelControl.AttributeDefs.current_level.id, level_zcl
                )
                C4.c4_suppress_level_sync(
                    self.endpoint.device, _LEVEL_SYNC_SUPPRESS_SECONDS
                )
                onoff = self.endpoint.in_clusters.get(OnOff.cluster_id)
                if onoff is not None:
                    onoff.update_attribute(
                        OnOff.AttributeDefs.on_off.id, level_zcl > 0
                    )
                # Cache the requested target level as on_level too, but
                # only from the command actually SENT — not from the
                # device's own c4.dm.t0c announcements
                # (c4_helpers.py's _sync_ep1_level), which fire repeatedly
                # while ramping and previously corrupted on_level with a
                # transient mid-ramp value on every off(), making the
                # light settle dimmer on each cycle (reverted; see
                # _sync_ep1_level's docstring). The command-time target is
                # what the user/HA actually asked for and is immune to
                # ramp timing.
                if level_zcl > 0:
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
                INPUT_CLUSTERS:  [C4DimmerButtonCluster],
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
            # Virtual per-button endpoints — one Event entity each in ZHA.
            # See C4DimmerButtonCluster / DIMMER_BUTTON_EVENT_EP_MAP.
            **{
                ep_id: {
                    PROFILE_ID:      zha.PROFILE_ID,
                    DEVICE_TYPE:     0x0000,
                    INPUT_CLUSTERS:  [_DIMMER_BUTTON_CLUSTERS[btn_name]],
                    OUTPUT_CLUSTERS: [],
                }
                for btn_name, ep_id in DIMMER_BUTTON_EVENT_EP_MAP.items()
            },
            # Virtual button/led-attached endpoints — one Switch entity
            # each in ZHA. See c4_attached_switch.py.
            ATTACHED_SWITCH_EP_MAP["button_attached"]: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4ButtonAttachedOnOff],
                OUTPUT_CLUSTERS: [],
            },
            ATTACHED_SWITCH_EP_MAP["led_attached"]: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4LedAttachedOnOff],
                OUTPUT_CLUSTERS: [],
            },
            # Virtual per-button LED-color endpoints — one RGB light
            # entity each in ZHA. See c4_led_rgb.py.
            LED_COLOR_EP_MAP["top"]: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4LedOnOff, C4TopLedColorCluster],
                OUTPUT_CLUSTERS: [],
            },
            LED_COLOR_EP_MAP["bottom"]: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4LedOnOff, C4BottomLedColorCluster],
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
    #
    # ENDPOINT_ID points at DIMMER_BUTTON_EVENT_EP_MAP's virtual per-button
    # endpoints, not at 197 (this cluster's own endpoint) — since
    # C4DimmerButtonCluster started firing zha_send_event there instead, to
    # also get a dedicated binary_sensor entity per button (see its
    # docstring). CLUSTER_ID must match the virtual endpoint's real
    # cluster identity — BinaryInput (0x000F, see c4_button_cluster.py's
    # _make_dimmer_button_cluster), not the C4_BUTTON_CLUSTER_ID used on
    # EP197 itself, since that's the cluster ZHA tags the fired zha_event
    # with (a stale value here would silently break trigger matching).
    # Iterates APD120_BUTTON_MAP (c4_helpers.py) — the real, confirmed
    # 0=top/1=bottom scheme for this device's c4.dm.* protocol — not the
    # shared DIMMER_BUTTON_MAP, which uses an unrelated older on/off-
    # button id scheme still needed by C4SwitchButtonCluster/
    # C4DualOutletButtonCluster for their own physical devices.
    device_automation_triggers = {
        (_action, _btn_name): {
            COMMAND: _action,
            CLUSTER_ID: BinaryInput.cluster_id,
            ENDPOINT_ID: DIMMER_BUTTON_EVENT_EP_MAP[_btn_name],
        }
        for _btn_id, _btn_name in APD120_BUTTON_MAP.items()
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
_LOGGER.info("C4 APD120: registered LDZ-101 dimmer aliases")
_LOGGER.info("C4 APD120: registered C4-APD120 in _C4_MODEL_QUIRK_MAP")
