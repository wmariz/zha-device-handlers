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

button_attached/led_attached are NOT implemented in this cluster. They
were first added here as ZCL commands, then as custom Bool attributes —
both confirmed broken on real hardware for producing a usable HA control:
a command never gets its own entity and rendered as a 0-255 slider (typed
uint8_t) in the "Issue command" UI, and a bare attribute on a fully
custom cluster gets no auto-generated entity either (ZHA only has
hardcoded platform support for a handful of core-recognized ZCL
attributes, not arbitrary manufacturer-specific ones) and showed as a
plain text box, not a toggle, in the Attributes UI. They now live in
c4_attached_switch.py as two virtual per-toggle endpoints, each a plain
OnOff cluster — the same virtual-endpoint trick used for this device's
button Event entities — since ZHA's switch-platform discovery reliably
turns any OnOff cluster into a real Switch entity. See that file's
docstring for detail.

Exported:
  C4RampCluster         — cluster with ramp-rate + hardware-config commands,
                          plus on_ramp_ms/off_ramp_ms attributes for the
                          Home Assistant "Ramp Rate Up"/"Ramp Rate Down"
                          Number config entities
  C4RampClusterOutlet2  — same Number entities, but for outlet 2 of a
                          dual-outlet dimmer (LOZ-5D1-W): purely a local
                          cache, never sent to the device — see its own
                          docstring below.
  C4_RAMP_CLUSTER_ID    — cluster ID (0xFC44)
  RAMP_IDX_*            — named constants for transition time indices
"""

import asyncio
import logging
import os
import sys
from typing import Final

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.quirks import CustomCluster
import zigpy.types as t
from zigpy.zcl import foundation
from zigpy.zcl.foundation import (
    BaseAttributeDefs,
    BaseCommandDefs,
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
from c4_led_rgb import _C4LocalOnlyReadMixin

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


def find_ramp_cluster(device, ep_id: int = 4):
    """Find a C4RampCluster (or subclass, e.g. C4RampClusterOutlet2) on the
    given endpoint of a device, if present. Shared by both C4DimmerOnOff
    (control4_dimmer.py) and the LevelControl classes in
    control4_dimmer.py/control4_outlet_dimmer.py so on/off and slider
    dimming read the exact same cached Ramp Rate.
    """
    ep = device.endpoints.get(ep_id)
    if ep is None:
        return None
    return ep.in_clusters.get(C4_RAMP_CLUSTER_ID)


# A move_to_level(_with_on_off) call ALWAYS carries a transition_time —
# even a plain dashboard brightness-slider drag with no explicit
# `transition:` still gets one, computed by zha's light platform from
# self._zha_config_transition (defaulting to _DEFAULT_MIN_TRANSITION_TIME
# = 0.1 s = 1 tenth — confirmed by reading zha/application/platforms/
# light/__init__.py directly). So transition_time alone can't tell "the
# user/automation explicitly asked for this transition" apart from "zha's
# own filler value" — only a call meaningfully ABOVE that filler is
# treated as an explicit override; anything at/near it falls back to the
# cached Ramp Rate Up/Down instead. 2 tenths (200 ms) sits above zha's
# 1-tenth filler with a small margin and comfortably below any sane
# configured Ramp Rate.
EXPLICIT_TRANSITION_THRESHOLD_TENTHS = 2


class C4RampCluster(_C4LocalOnlyReadMixin, CustomCluster):
    """Ramp/transition time cluster for Control4 dimmers.

    Provides commands to read and write dimmer ramp rates via the C4
    serial-over-ZigBee protocol (c4.dm.tv namespace). The related
    button/LED-attached hardware-config toggles (c4.dm.ba / c4.dm.lm),
    which Composer groups with the ramp rates under the same "Wireless
    Dimmer" driver panel, live in c4_attached_switch.py instead — see
    this module's own docstring for why.

    The cluster caches the current ramp times locally so that
    C4DimmerOnOff can read them for on/off transition commands.

    on_ramp_ms/off_ramp_ms are exposed as real ZCL attributes (added on
    top of the pre-existing set_on_ramp/set_off_ramp commands, kept
    unchanged) purely so QuirkBuilder's .number() can bind Home
    Assistant "Ramp Rate Up"/"Ramp Rate Down" Number config entities to
    them — writing either one calls the exact same _send_ramp_set() the
    old commands already used. Reads never touch the wire (see
    _C4LocalOnlyReadMixin, c4_led_rgb.py): there's nothing real to read,
    only the local cache _send_ramp_set() keeps in sync.

    Usage from Home Assistant (via zha.issue_zigbee_cluster_command):
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
    """

    cluster_id = C4_RAMP_CLUSTER_ID
    name = "Control4 Ramp Control"
    ep_attribute = "c4_ramp_control"
    _c4_custom_handler = True

    # c4.dm.tv channel byte this instance sends Set commands on. 00 is the
    # only value ever captured on the wire (single-output APD120/LDZ-101 and
    # outlet 1 of the LOZ-5D1-W). See C4RampClusterOutlet2 for the outlet-2
    # variant.
    CHANNEL = 0x00

    # Endpoint whose LevelControl on/off transition attrs get synced from
    # this cluster's cache — the endpoint hosting the OnOff/LevelControl
    # pair this ramp cluster actually controls.
    _SYNC_EP_ID = 1

    # Local cache: index → time in ms
    _ramp_times: dict[int, int] = {}

    # Delays (seconds) between retries of _push_ramp_defaults() after the
    # first (immediate) attempt — see that method's docstring for why a
    # retry is needed at all. CONFIRMED on real hardware (HA debug log)
    # that a shorter (3, 8, 20) schedule wasn't always enough — one boot
    # took ~35s for the ApplicationController to come up, 2s past that
    # schedule's ~31s total budget, and the push gave up right before it
    # would have succeeded. This schedule's cumulative offsets are
    # 0/5/20/50/110s, comfortably outlasting that observed case.
    _RAMP_DEFAULTS_RETRY_DELAYS = (5, 15, 30, 60)

    class AttributeDefs(BaseAttributeDefs):
        """Purely local attributes — see class docstring."""

        on_ramp_ms: Final = ZCLAttributeDef(
            id=0x0000, type=t.uint16_t, is_manufacturer_specific=True,
        )
        off_ramp_ms: Final = ZCLAttributeDef(
            id=0x0001, type=t.uint16_t, is_manufacturer_specific=True,
        )

    # Default seed for the two user-facing Number entities (on_ramp_ms/
    # off_ramp_ms): 750 ms for BOTH, by explicit user request ("750ms is
    # Control4's default"). This intentionally differs from
    # RAMP_DEFAULTS_MS[RAMP_IDX_OFF] (2000 ms) — that dict documents the
    # real APD120 provisioning-time capture for all 9 indices and is left
    # untouched for its other uses (get_off_ramp_tenths()'s no-cluster
    # fallback, get_ramp_ms()'s fallback); only the entity-facing seed
    # below is overridden.
    _ENTITY_DEFAULT_ON_MS = 750
    _ENTITY_DEFAULT_OFF_MS = 750

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Initialize cache with defaults
        self._ramp_times = dict(RAMP_DEFAULTS_MS)
        self._ramp_times[RAMP_IDX_ON] = self._ENTITY_DEFAULT_ON_MS
        self._ramp_times[RAMP_IDX_OFF] = self._ENTITY_DEFAULT_OFF_MS
        self._update_attribute(
            self.AttributeDefs.on_ramp_ms.id, self._ramp_times[RAMP_IDX_ON]
        )
        self._update_attribute(
            self.AttributeDefs.off_ramp_ms.id, self._ramp_times[RAMP_IDX_OFF]
        )
        # Push the defaults to the device on every HA startup — see
        # _push_ramp_defaults()'s own docstring for why this is needed and
        # why it's scheduled from here rather than a framework hook.
        asyncio.ensure_future(self._push_ramp_defaults())

    async def _push_ramp_defaults(self):
        """Send the just-initialized ramp times to the device.

        CONFIRMED gap on real hardware: without this, __init__ only seeds
        the LOCAL ZCL attribute cache with RAMP_DEFAULTS_MS — the Number
        entities show 750/2000 ms in Home Assistant, but nothing is ever
        actually sent to the device until the user explicitly edits a
        value. After a restart, the device's own hardware state (whatever
        it was left at before — its own factory value, or a value set in
        a prior HA session) silently drifts out of sync with whatever HA
        displays, since this quirk has no cross-restart storage of "the
        last value the user set" — only zigpy's own in-memory attribute
        cache, which __init__ always resets to RAMP_DEFAULTS_MS on every
        fresh process. Sending here keeps the device and the UI in
        agreement, and explicitly applies Control4's own 750 ms/2000 ms
        on-ramp/off-ramp default (rather than leaving it a cosmetic-only
        UI value) every time HA starts.

        Scheduled via asyncio.ensure_future() from __init__ instead of a
        zigpy/zha framework "device ready" hook: this fork's quirks all
        call .skip_configuration() (see every QuirkBuilder chain in this
        package), which short-circuits zha's own
        Device.async_initialize()/initialize_cluster_configs() path
        entirely — confirmed by reading zha/zigbee/device.py in the real
        zha==2.2.2 package: `if aggregated and not self.skip_configuration`
        never runs when skip_configuration is set. A prior version of
        this method tried overriding zigpy.zcl.Cluster.async_initialize()
        directly (mirroring c4_basic_cluster.py's C4BasicCluster), but
        that method doesn't exist at all on Cluster in the installed
        zigpy==2.2.0 (confirmed via inspecting the MRO directly) — calling
        super().async_initialize() raised AttributeError immediately in
        testing. Firing here instead mirrors the already-proven pattern
        in c4_button_cluster.py's C4KeypadButtonCluster (schedule via
        asyncio.ensure_future from a sync context) rather than relying on
        an initialization hook that turns out not to fire for this fork's
        devices. _send_ramp_set() already wraps its own device.request()
        in try/except, so a failure here (e.g. the network stack not
        being ready yet at construction time) just logs a warning instead
        of raising — same risk profile as today, not a regression.

        On C4RampClusterOutlet2 (outlet 2, local-only — see its own
        docstring/_send_ramp_set override), this only re-affirms the
        local cache; it never touches the wire.

        CONFIRMED broken on real hardware (real HA debug log): the very
        first attempt right after an HA restart reliably fails with
        "C4 Ramp: failed to set on_ramp to 750 ms — ApplicationController
        is not running" — the zigbee radio isn't ready yet at the exact
        moment __init__ schedules this task, so the push silently never
        reached the device on a normal boot despite this method existing.
        Retries with increasing delays (_RAMP_DEFAULTS_RETRY_DELAYS) until
        both sends report success, rather than firing once and giving up.

        Also CONFIRMED (user report + reading zigpy/appdb.py directly):
        a value the user explicitly sets during one HA session did not
        survive a restart, always reverting to the 750 ms default — not
        because zigpy fails to persist it (its appdb persists ANY
        _update_attribute() call generically, via AttributeUpdatedEvent,
        for any cluster/attribute), but because __init__ above always
        seeds self._ramp_times with the hardcoded default BEFORE zigpy's
        own restore pass has necessarily run (appdb.load() clears every
        quirked cluster's attribute cache and repopulates it from the
        database in two passes that happen to straddle device/quirk
        resolution — this __init__ runs in between them), and this
        method used to blindly push self._ramp_times' hardcoded value
        regardless, both overwriting the correctly-restored in-memory
        cache AND re-persisting 750 back to the database on every
        successful send — a self-reinforcing loop that made a custom
        value impossible to keep. Now checks self.get() for both
        attributes on every attempt (not just once): by the time a
        retry actually fires, zigpy's local SQLite restore (fast, no
        relation to how long the zigbee radio itself takes to come up)
        will certainly have completed, so any already-restored value is
        adopted into self._ramp_times before pushing, instead of being
        clobbered by the hardcoded default.
        """
        delays = (0,) + self._RAMP_DEFAULTS_RETRY_DELAYS
        for attempt, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)

            cached_on = self.get(self.AttributeDefs.on_ramp_ms.id)
            if cached_on is not None:
                self._ramp_times[RAMP_IDX_ON] = int(cached_on)
            cached_off = self.get(self.AttributeDefs.off_ramp_ms.id)
            if cached_off is not None:
                self._ramp_times[RAMP_IDX_OFF] = int(cached_off)

            on_ok = await self._send_ramp_set(
                RAMP_IDX_ON, self._ramp_times[RAMP_IDX_ON]
            )
            off_ok = await self._send_ramp_set(
                RAMP_IDX_OFF, self._ramp_times[RAMP_IDX_OFF]
            )
            if on_ok and off_ok:
                return
            _LOGGER.debug(
                "C4 Ramp: default push attempt %d failed (on_ok=%s "
                "off_ok=%s), will retry", attempt + 1, on_ok, off_ok,
            )
        _LOGGER.warning(
            "C4 Ramp: giving up pushing defaults to the device after %d "
            "attempts", len(delays),
        )

    async def write_attributes(self, attributes, manufacturer=None):
        """Translate a Number entity write into the same wire Set command
        set_on_ramp/set_off_ramp already send — see class docstring.
        """
        for attr, value in attributes.items():
            attr_id = (
                self.find_attribute(attr).id if isinstance(attr, str) else attr
            )
            if attr_id == self.AttributeDefs.on_ramp_ms.id:
                await self._send_ramp_set(RAMP_IDX_ON, int(value))
            elif attr_id == self.AttributeDefs.off_ramp_ms.id:
                await self._send_ramp_set(RAMP_IDX_OFF, int(value))
            else:
                _LOGGER.debug(
                    "C4 Ramp: ignoring write to unrecognized attr 0x%04X = %s",
                    attr_id, value,
                )
        return [[foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS)]]

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

    async def _send_ramp_set(self, index: int, time_ms: int) -> bool:
        """Send a c4.dm.tv Set command to change a transition time.

        Returns True if the wire send didn't raise, False otherwise —
        used by _push_ramp_defaults() to decide whether a retry is
        needed (see its docstring: the very first attempt right after
        an HA restart reliably fails with "ApplicationController is not
        running", confirmed on real hardware).
        """
        device = self.endpoint.device
        name = RAMP_INDEX_NAMES.get(index, f"0x{index:02x}")

        # Clamp to uint16 range
        time_ms = max(0, min(65535, time_ms))

        seq = next_c4_seq(device)
        cmd = f"0s{seq:04x} c4.dm.tv {self.CHANNEL:02x} {index:02x} {time_ms:04x}"

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

            # Sync ZCL LevelControl transition attributes on _SYNC_EP_ID
            self._sync_zcl_transition_attrs()

            # Keep the Number config entities' own cached value in sync,
            # regardless of which path (command or attribute write) got here.
            if index == RAMP_IDX_ON:
                self._update_attribute(self.AttributeDefs.on_ramp_ms.id, time_ms)
            elif index == RAMP_IDX_OFF:
                self._update_attribute(self.AttributeDefs.off_ramp_ms.id, time_ms)
            return True

        except Exception as e:
            _LOGGER.warning(
                "C4 Ramp: failed to set %s to %d ms — %s", name, time_ms, e,
            )
            return False

    def _sync_zcl_transition_attrs(self):
        """Push cached ramp times into _SYNC_EP_ID's LevelControl attribute cache.

        This keeps ZHA's attribute display consistent with the actual device
        ramp times and ensures C4DimmerOnOff uses the correct values.
        """
        try:
            from zigpy.zcl.clusters.general import LevelControl

            sync_ep = self.endpoint.device.endpoints.get(self._SYNC_EP_ID)
            if sync_ep is None:
                return
            level_cluster = sync_ep.in_clusters.get(LevelControl.cluster_id)
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


class C4RampClusterOutlet2(C4RampCluster):
    """Local-only ramp-time cache for outlet 2 of a dual-outlet dimmer (LOZ-5D1-W).

    Outlet 2 (synthetic EP11) has no real ZCL circuit and no confirmed
    persistent "transition time" table of its own on the wire (unlike
    outlet 1/the plain APD120, where c4.dm.tv channel 00 is confirmed to
    both read and durably store 9 transition-time parameters on the
    device itself). Disassembling the real driver
    (outlet_ip_control4.c4l — SetRampRate(SingleOutletInfo*, int,
    RampTypes)) confirmed Control4's own analogous "Hold/Click Ramp Rate"
    Composer fields are NEVER sent to the device either: the function
    only validates/cascades the paired value and notifies the Composer
    UI — no call into SendMIBPacketWithHexParams/SendZclPacket, and no
    dedicated MIB variable-name string exists for it (every wire-facing
    feature in that driver has one, e.g. s_MIBRampToLevelStr;
    ramp-rate has none). The controller instead uses the locally-cached
    value purely to pace its OWN sequence of live ramp commands.

    This class replicates that: writing "2 Ramp Rate Up"/"2 Ramp Rate
    Down" (on_ramp_ms/off_ramp_ms) only updates the local cache — nothing
    is sent to the device. C4Outlet2DimmerLevelControl/C4Outlet2OnOff
    (control4_outlet_dimmer.py) read the cache via get_ramp_ms() and use
    it as the <time_ms> argument of a c4.dm.rtl RAMP_TO_LEVEL command
    whenever outlet 2's level actually changes, instead of the old
    instant c4.dm.tv SET_LEVEL send.
    """

    async def _send_ramp_set(self, index: int, time_ms: int) -> bool:
        """Cache locally only — see class docstring. Overrides the base
        class's wire-sending implementation entirely; this covers both
        Number-entity writes (write_attributes) and the inherited
        set_ramp_rate/set_on_ramp/set_off_ramp ZCL commands, since all of
        them funnel through this method. Always returns True (nothing to
        retry — there's no wire send that could fail here).
        """
        time_ms = max(0, min(65535, int(time_ms)))
        self._ramp_times[index] = time_ms
        if index == RAMP_IDX_ON:
            self._update_attribute(self.AttributeDefs.on_ramp_ms.id, time_ms)
        elif index == RAMP_IDX_OFF:
            self._update_attribute(self.AttributeDefs.off_ramp_ms.id, time_ms)
        return True
