"""C4 button clusters — shared across dimmer, switch, outlet, keypad.

Classes exported:
  C4ButtonCluster                  — base, used by plain switches
  C4DimmerButtonCluster            — C4-APD120/LDZ-101 dimmer (adds per-button binary_sensor entities)
  C4SwitchButtonCluster            — on/off switch variant
  C4SwitchButtonClusterWithBinarySensor — LSZ-101 (adds per-button binary_sensor entities)
  C4DualOutletButtonCluster        — LOZ-5S1-W dual outlet
  C4KeypadButtonCluster            — C4-KPZ-6B1 6-button keypad (adds per-button binary_sensor entities)
  _DIMMER_BUTTON_CLUSTERS          — per-button virtual cluster dict for the dimmer (name → class)
  _KPZ6B1_BUTTON_CLUSTERS          — per-button virtual cluster dict for the KPZ-6B1
  _make_dimmer_button_cluster()    — factory for the dimmer's per-button BinaryInput cluster
  _make_keypad_button_cluster()    — factory for the KPZ-6B1's per-button BinaryInput cluster
  _make_binary_button_cluster()    — shared base factory both of the above delegate to
"""

import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.quirks import CustomCluster
from zigpy.zcl.clusters.general import BinaryInput, LevelControl, OnOff

from zhaquirks import EventableCluster
from zhaquirks.const import (
    BUTTON,
    DOUBLE_PRESS,
    ENDPOINT_ID,
    LONG_RELEASE,
    SHORT_PRESS,
    TRIPLE_PRESS,
    QUADRUPLE_PRESS,
)

import asyncio

import c4_helpers as C4
from c4_helpers import (
    APD120_BUTTON_MAP,
    C4_BUTTON_CLUSTER_ID,
    C4_CLUSTER_ID,
    C4_PROFILE_BUTTON,
    C4_PROVISION_DELAY,
    DIMMER_BUTTON_EVENT_EP_MAP,
    DIMMER_BUTTON_MAP,
    DIMMER_EVENT_MAP,
    KEYPAD_EVENT_MAP,
    KPZ6B1_BUTTON_EP_MAP,
    KPZ6B1_BUTTON_MAP,
    OUTLET_EP_MAP,
    _build_c4_frame,
    _sync_ep1_level,
    _sync_ep1_onoff,
    c4_spawn,
    next_c4_seq,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base button cluster
# ---------------------------------------------------------------------------

class C4ButtonCluster(EventableCluster):
    """Button events from C4 devices on endpoint 197 (0xC5).

    Handles state announcements from the C4 serial-over-Zigbee protocol.
    Subclasses override BUTTON_MAP and/or individual event handlers.
    """

    cluster_id   = C4_BUTTON_CLUSTER_ID
    name         = "Control4 Button Events"
    ep_attribute = "c4_buttons"
    _c4_custom_handler = True

    BUTTON_MAP = DIMMER_BUTTON_MAP

    # ------------------------------------------------------------------
    # Message entry points
    # ------------------------------------------------------------------

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        _LOGGER.debug("C4 button request: hdr=%s args=%s", hdr, args)
        self._process_raw(hdr, args)

    def handle_message(self, hdr, args):
        self._process_raw(hdr, args)

    # ------------------------------------------------------------------
    # Raw frame parser
    # ------------------------------------------------------------------

    def _process_raw(self, hdr, args):
        raw_bytes = None
        if isinstance(args, (bytes, bytearray)):
            raw_bytes = bytes(args)
        elif args and isinstance(args, (list, tuple)):
            if isinstance(args[0], (bytes, bytearray)):
                raw_bytes = bytes(args[0])
            elif isinstance(args[0], int):
                raw_bytes = bytes(args)
            elif isinstance(args[0], (list, tuple)):
                raw_bytes = bytes(args[0])

        if raw_bytes is None:
            _LOGGER.warning(
                "C4 button: cannot extract bytes: args=%s type=%s",
                args, type(args),
            )
            return

        text = raw_bytes.decode("ascii", errors="replace").strip()
        _LOGGER.debug("C4 button text: %r", text)

        cmd = text.split()
        if len(cmd) >= 3:
            prefix = cmd[0][0:2]
            if prefix == "0t":
                msg_type = "announce"
            elif prefix == "0r":
                msg_type = "report"
            elif prefix == "0s":
                msg_type = "set"
            elif prefix == "0g":
                msg_type = "get"
            elif prefix == "0i":
                msg_type = "initialize"
            else:
                msg_type = prefix

            if cmd[1] == "sa":
                self._handle_state_announcement(cmd[2], cmd[3:])
                return
            elif msg_type == "report":
                _LOGGER.debug("C4 report: %s", text)
                return

            self.listener_event(
                "zha_send_event",
                {
                    "command": "raw_button_frame",
                    "params": {
                        "type": msg_type,
                        "sequence": cmd[0][2:] if len(cmd[0]) > 2 else None,
                        "namespace": cmd[1] if len(cmd) > 2 else None,
                        "data": cmd[2:] if len(cmd) > 3 else None,
                        "raw_text": text,
                    },
                },
            )

    # ------------------------------------------------------------------
    # State announcement dispatcher
    # ------------------------------------------------------------------

    def _handle_state_announcement(self, namespace, data):
        _LOGGER.debug("C4 state: %s data='%s'", namespace, data)

        if namespace == "c4.dmx.dim":
            self._handle_dim_level(data)
        elif namespace == "c4.dmx.ls":
            self._handle_light_state(data)
        elif namespace == "c4.dm.t0c":
            self._handle_t0c_level(data)
        elif namespace == "c4.dmx.warn":
            # Device-side warning is unusual — surface at INFO so it's visible.
            _LOGGER.info("C4 state: warning = %s", data)
        elif namespace == "c4.dmx.amb":
            _LOGGER.debug("C4 state: ambient = %s", data)
        elif namespace == "c4.dmx.bp":
            if data:
                self._handle_button_event(namespace, data[0])
        elif namespace == "c4.dmx.cc":
            if len(data) >= 2:
                _LOGGER.debug(
                    "C4 state: click count, button = %s, clicks = %s",
                    data[0], data[1],
                )
                self._handle_button_event(namespace, data[0], data[1])
        elif namespace == "c4.dm.cc":
            # CONFIRMED from a real HA debug log capture on an LDZ-101
            # dimmer: this device's actual click-count announcements use
            # the single-channel `c4.dm.*` family (like c4.dm.t0c for
            # level), not `c4.dmx.cc` — every real button press was
            # silently falling through to "unknown namespace" below.
            # Identical shape/semantics to c4.dmx.cc otherwise (button,
            # click count), so _handle_button_event's event_code
            # resolution (namespace.split(".")[-1] == "cc") works
            # unchanged.
            if len(data) >= 2:
                _LOGGER.debug(
                    "C4 state: click count, button = %s, clicks = %s",
                    data[0], data[1],
                )
                self._handle_button_event(namespace, data[0], data[1])
        elif namespace == "c4.dmx.hc":
            _LOGGER.debug("C4 state: hold, button = %s", data[0])
            self._handle_button_event(namespace, data[0])
        elif namespace == "c4.dmx.he":
            _LOGGER.debug("C4 state: release after hold, button = %s", data[0])
            self._handle_button_event(namespace, data[0])
        elif namespace == "c4.dmx.sc":
            if data:
                _LOGGER.debug("C4 state: scene change, button = %s", data[0])
                self._handle_button_event(namespace, data[0])
        elif namespace == "c4.dmx.tc":
            # CONFIRMED bug: this only ever passed data[0] (button) through,
            # never data[1] — but _sync_state_from_event's "tc" branch
            # expects params["extra_value"] (the level reached once the
            # transition finished) to call _sync_ep1_level, and that key is
            # only populated when _handle_button_event receives a third
            # (extra) argument. Level was silently never synced from this
            # event. Fixed by passing data[1] through, same as c4.dmx.cc.
            if len(data) >= 2:
                _LOGGER.debug(
                    "C4 state: transition complete, button = %s, level = %s",
                    data[0], data[1],
                )
                self._handle_button_event(namespace, data[0], data[1])
            elif data:
                _LOGGER.debug(
                    "C4 state: transition complete, button = %s (no level "
                    "field)", data[0],
                )
                self._handle_button_event(namespace, data[0])
        elif namespace.startswith("c4.dm.b") and len(namespace) == 9:
            self._handle_dm_b_code(namespace)
        elif namespace in ("c4.kp.bb", "c4.kp.bh", "c4.kp.be"):
            # KPZ-6B1 keypad — CONFIRMED from a real HC300 controller log:
            # bb=press-begin, bh=hold (fires while held), be=hold-end
            # (release after a hold). data[0] = button id (hex, 0-5).
            if data:
                self._handle_button_event(namespace, data[0])
        elif namespace == "c4.kp.bc":
            # KPZ-6B1 — click complete (quick release, before the count is
            # known). No separate action needed: the always-following
            # c4.kp.cc carries the resolved click count.
            _LOGGER.debug("C4 state: keypad click, button = %s", data[0] if data else "?")
        elif namespace == "c4.kp.cc":
            # KPZ-6B1 — click count confirmation, same shape as c4.dmx.cc/
            # c4.dm.cc (button, click count).
            if len(data) >= 2:
                self._handle_button_event(namespace, data[0], data[1])
        elif namespace == "c4.zr.bb":
            # SR260 remote — button begin (key down). data[0] = button id (hex).
            if data:
                self._handle_button_event(namespace, data[0])
        elif namespace == "c4.zr.be":
            # SR260 remote — button end (key up).
            if data:
                self._handle_button_event(namespace, data[0])
        elif namespace in ("c4.zr.mot", "c4.zr.bl", "c4.zr.tm", "c4.zr.loc"):
            # SR260 wake / backlight / clock / locale — protocol-level only,
            # no HA event needed.
            _LOGGER.debug("C4 state: %s data=%s (no event)", namespace, data)
        elif namespace.startswith("c4.ln."):
            # SR260 LCD / list-rendering protocol — handled separately if/when
            # screen support is added. Silence here so it does not log as
            # "unknown" on every menu interaction.
            _LOGGER.debug(
                "C4 state: ignoring screen command %s data=%s", namespace, data
            )
        else:
            # Unknown namespace from a known device — keep at INFO so the user
            # sees the protocol field that is going unhandled.
            _LOGGER.info(
                "C4 state: unknown namespace '%s', data = %s", namespace, data
            )

    # ------------------------------------------------------------------
    # Button event handler
    # ------------------------------------------------------------------

    def _handle_dm_b_code(self, namespace):
        """Handle the c4.dm.b<button_hex><code> family (APD120/LDZ-101).

        CONFIRMED from two clean, isolated real HA debug log captures on
        an LDZ-101 (one click on each button, then a ~2s hold): unlike
        every other namespace in this protocol, the button id AND the
        event code are both encoded directly in the namespace itself
        (c4.dm.b0c, c4.dm.b1b, ...), with no data fields at all — so this
        can't reuse _handle_button_event's normal "code after the last
        dot, button/extra as data fields" shape.

        Two codes confirmed:
          c — click-begin: fires the instant the button goes down,
              followed ~1.5-2s later by the usual c4.dm.cc click-count
              confirmation (see _handle_state_announcement) once the
              device's click-debounce window closes.
          b / e — hold-begin / hold-end: 'b' fires immediately on press
              (same instant as 'c' would for a plain click — the device
              can't yet know which one it'll become), 'e' fires only
              once the button is actually released, however long it was
              held.
        Since 'c' and 'b' both mark "the button just went down" before
        the outcome is known, both map to the same "press" action; 'e'
        maps to LONG_RELEASE, which _fire_button_zha_event already
        treats as a release (turns the binary_sensor back off), same as
        the click-count confirmation does for a plain click.
        """
        button_hex, code = namespace[7], namespace[8]
        try:
            button_id = int(button_hex, 16)
        except ValueError:
            _LOGGER.warning("C4 state: invalid button in %r", namespace)
            return
        button_name = self.BUTTON_MAP.get(button_id, f"button_{button_id:#04x}")

        if code in ("c", "b"):
            action = "press"
        elif code == "e":
            action = LONG_RELEASE
        else:
            _LOGGER.info(
                "C4 state: unknown b-code %r in %r — data unhandled",
                code, namespace,
            )
            return

        _LOGGER.debug(
            "C4 button event: button=%s event=%s (namespace=%s)",
            button_name, action, namespace,
        )
        self._fire_button_zha_event(action, button_id, button_name)

    def _handle_button_event(self, namespace, button, extra=None):
        button_id = int(button, 16)
        button_name = self.BUTTON_MAP.get(button_id, f"button_{button_id:#04x}")
        event_code = namespace.split('.')[-1] if namespace else "unknown"
        action = self._resolve_action(event_code, extra)

        params = {"event_code": event_code, "button_id": button_id}
        if extra is not None:
            params["extra_value"] = int(extra, 16)
            if event_code == "cc":
                params["click_count"] = params["extra_value"]

        _LOGGER.debug(
            "C4 button event: button=%s event=%s", button_name, event_code
        )
        self._sync_state_from_event(event_code, button_id, params)
        self._fire_button_zha_event(action, button_id, button_name)

    # Overridable per-subclass (e.g. C4KeypadButtonCluster's KEYPAD_EVENT_MAP)
    # — "bb" means something different on the KPZ-6B1 (press-begin) than it
    # does here for the SR260 (a complete short press), so a shared map
    # across every C4ButtonCluster subclass isn't safe once a new device
    # reuses a letter code with different semantics.
    EVENT_MAP = DIMMER_EVENT_MAP

    def _resolve_action(self, event_code, extra):
        """Map an event_code (+ optional click-count extra) to a zha_event action."""
        action = self.EVENT_MAP.get(event_code, f"unknown_{event_code}")
        if action == "click_count" and extra is not None:
            if extra == "01":
                action = SHORT_PRESS
            elif extra == "02":
                action = DOUBLE_PRESS
            elif extra == "03":
                action = TRIPLE_PRESS
            else:
                action = QUADRUPLE_PRESS
        return action

    def _fire_button_zha_event(self, action, button_id, button_name):
        """Fire the zha_event for a resolved button action.

        Split out from _handle_button_event so a subclass can redirect
        where the event fires (e.g. C4DimmerButtonCluster below, which
        routes to a dedicated per-button virtual endpoint instead of
        firing on this cluster's own endpoint) without duplicating the
        action-resolution logic above.
        """
        self.listener_event(
            "zha_send_event",
            action,
            {
                BUTTON: button_name,
                ENDPOINT_ID: self.endpoint.endpoint_id,
            },
        )

    # ------------------------------------------------------------------
    # Light state / dim level helpers
    # ------------------------------------------------------------------

    def _handle_light_state(self, fields):
        """Parse c4.dmx.ls multi-field light state; field[2] = 0–100 % (hex)."""
        try:
            if len(fields) >= 3:
                level_pct = int(fields[2], 16)
                zcl_level = round(level_pct * 254 / 100) if level_pct > 0 else 0
                _LOGGER.debug(
                    "C4 state: ls level=%d%% → zcl=%d", level_pct, zcl_level
                )
                _sync_ep1_level(self.endpoint.device, zcl_level, "0t_dmx_ls")
        except (ValueError, IndexError) as e:
            _LOGGER.warning("C4 state: failed to parse ls: '%s' (%s)", fields, e)

    def _handle_dim_level(self, data):
        """Handle c4.dmx.dim; data[0] = 0–100 % encoded as a hex byte."""
        try:
            level_pct = int(data[0], 16)
            zcl_level = round(level_pct * 254 / 100) if level_pct > 0 else 0
            _LOGGER.debug(
                "C4 state: dim level=%d%% → zcl=%d", level_pct, zcl_level
            )
            _sync_ep1_level(self.endpoint.device, zcl_level, "c4.dmx.dim")
        except (ValueError, IndexError, TypeError):
            _LOGGER.warning("C4: failed to parse dim level: '%s'", data)

    def _handle_t0c_level(self, data):
        """Handle c4.dm.t0c; data[0] = 0-100% (0-0x64) encoded as a hex byte.

        CONFIRMED from a real HA debug log capture on an LDZ-101 (uses this
        same C4ButtonCluster via the Control4APD120Dimmer quirk): the
        physical dimmer's live level confirmations use this single-channel
        verb — channel 0 baked directly into the verb name itself, unlike
        the dual-outlet family's `c4.dm.tc <channel> <level>`, which needs
        an explicit channel argument since it serves two outlets. This
        namespace was previously unhandled (fell through to "unknown
        namespace", logged and dropped), so current_level never reflected
        a live physical dim and stayed stuck at whatever value the initial
        pairing interview happened to report — e.g. a plain on() via
        C4DimmerOnOff._get_on_level() (control4_dimmer.py) kept restoring
        that same stale value (observed: level 2, i.e. ~1%) instead of the
        light's actual last dimmed level, no matter how the light had last
        been set. _sync_ep1_level also caches on_level now (see its own
        docstring), so this — like the LOZ-5D1-W outlet dimmer — restores
        the real last level on a plain on() rather than a stale or fixed
        one.
        """
        try:
            level_pct = int(data[0], 16)
            zcl_level = round(level_pct * 254 / 100) if level_pct > 0 else 0
            _LOGGER.debug(
                "C4 state: t0c level=%d%% → zcl=%d", level_pct, zcl_level
            )
            _sync_ep1_level(self.endpoint.device, zcl_level, "c4.dm.t0c")
        except (ValueError, IndexError, TypeError):
            _LOGGER.warning("C4: failed to parse t0c level: '%s'", data)

    # ------------------------------------------------------------------
    # State sync helpers
    # ------------------------------------------------------------------

    def _sync_state_from_event(self, event_code, button_id, params):
        click_count = params.get("click_count")
        if event_code == "cc" and click_count is not None:
            self._sync_cc_event(button_id, click_count)
        elif event_code == "tc":
            extra = params.get("extra_value")
            if extra is not None:
                # CONFIRMED bug: extra is the raw 0-100% hex byte from
                # c4.dmx.tc's second field (same convention as every other
                # dim-level source in this protocol family — c4.dmx.dim,
                # c4.dmx.ls, c4.dm.t0c), but this passed it to
                # _sync_ep1_level completely unscaled, as if it were
                # already a 0-254 ZCL level — e.g. 80 (meaning 80%) would
                # have been written as current_level=80 (~31%) instead of
                # ~204 (80%). This call was unreachable until the
                # c4.dmx.tc dispatch fix (see _handle_state_announcement)
                # started actually passing extra through, so the wrong
                # scale was never observed in practice; fixed before it
                # could be.
                zcl_level = round(extra * 254 / 100) if extra > 0 else 0
                _sync_ep1_level(self.endpoint.device, zcl_level, "tc_event")

    def _sync_cc_event(self, button_id, click_count):
        """Sync on/off from c4.dmx.cc click-count confirmation."""
        try:
            ep1 = self.endpoint.device.endpoints.get(1)
            if ep1 is None:
                return
            onoff_cluster  = ep1.in_clusters.get(OnOff.cluster_id)
            level_cluster  = ep1.in_clusters.get(LevelControl.cluster_id)

            if button_id == 0x01 and click_count >= 1:
                _LOGGER.debug("C4 button sync: ON confirmed (btn=0x01 cc=%d)", click_count)
                if onoff_cluster is not None:
                    onoff_cluster.update_attribute(
                        OnOff.AttributeDefs.on_off.id, True
                    )
            elif button_id == 0x05 and click_count >= 1:
                _LOGGER.debug("C4 button sync: OFF confirmed (btn=0x05 cc=%d)", click_count)
                if onoff_cluster is not None:
                    onoff_cluster.update_attribute(
                        OnOff.AttributeDefs.on_off.id, False
                    )
                if level_cluster is not None:
                    level_cluster.update_attribute(
                        LevelControl.AttributeDefs.current_level.id, 0
                    )
        except Exception:
            _LOGGER.warning("C4 button sync: failed", exc_info=True)


# ---------------------------------------------------------------------------
# Dimmer variant — adds a dedicated sensor entity per physical button
# ---------------------------------------------------------------------------

def _make_binary_button_cluster(display_name: str, class_name: str) -> type:
    """Return a unique BinaryInput-based per-button virtual cluster.

    Shared by _make_dimmer_button_cluster (dimmer/switch top/bottom
    buttons) and _make_keypad_button_cluster (KPZ-6B1's 6 buttons) — the
    exact same class was independently confirmed correct for both device
    families, differing only in `name`/generated class name, so it's
    built once here and both factories just supply those two strings.

    CONFIRMED WRONG on real hardware, twice: (1) a bare EventableCluster,
    on the (unverified) assumption that ZHA creates a dedicated Event
    entity for it the same way the KC120277 scene controller's docstring
    claimed — it doesn't, EventableCluster's only real behavior is firing
    the classic zha_event BUS message, not creating an entity. (2) A plain
    MultistateInput cluster with a custom `ep_attribute` — still no entity
    appeared after a full reload, with zero disabled entities either, so
    the cluster type wasn't the (only) problem.

    Root cause, found in this same file's sibling
    control4_z2io_zp.py (C4ContactCluster, a CONFIRMED-working
    BinaryInput-backed binary_sensor on this exact device family):
    `ep_attribute` MUST stay the ZCL cluster's own inherited default
    (here, BinaryInput's "binary_input") for ZHA's ClusterHandler
    discovery to resolve `endpoint.binary_input` and create the entity —
    overriding it with a custom per-button name (as both earlier
    attempts did) silently breaks discovery. Each button still gets its
    own independent entity because it lives on its own dedicated virtual
    endpoint, not because of a unique ep_attribute.

    Uses BinaryInput instead of MultistateInput per the user's own
    suggestion: present_value=True on press, False on release/click-
    resolution reads naturally as a binary_sensor's on/off state, which
    is simpler to consume than an incrementing counter.
    """

    class _ButtonCluster(CustomCluster, BinaryInput):
        cluster_id   = BinaryInput.cluster_id
        name         = display_name
        _c4_custom_handler = False  # no physical routing

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # Seed a real value instead of leaving the entity "unknown"
            # until the first press.
            self._update_attribute(
                BinaryInput.AttributeDefs.present_value.id, False,
            )

        def handle_message(self, hdr, args):
            pass  # no physical packets arrive here

        def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
            pass

        def set_pressed(self, pressed: bool):
            """Set present_value — True while held, False once released."""
            self._update_attribute(
                BinaryInput.AttributeDefs.present_value.id, pressed,
            )

    _ButtonCluster.__name__     = class_name
    _ButtonCluster.__qualname__ = class_name
    return _ButtonCluster


def _make_dimmer_button_cluster(button_name: str) -> type:
    """Return _make_binary_button_cluster's class for one dimmer/switch button.

    See _make_binary_button_cluster's docstring for the full "two wrong
    turns" history behind this shape. DIMMER_BUTTON_EVENT_EP_MAP keys
    ("top"/"bottom") give each button its own virtual endpoint.
    """
    return _make_binary_button_cluster(
        f"{button_name.capitalize()} Button",
        f"C4Dimmer{button_name.capitalize()}ButtonCluster",
    )


# One cluster class per dimmer button — keyed by button name ("top"/"bottom")
_DIMMER_BUTTON_CLUSTERS: dict[str, type] = {
    btn_name: _make_dimmer_button_cluster(btn_name)
    for btn_name in DIMMER_BUTTON_EVENT_EP_MAP
}


class C4DimmerButtonCluster(C4ButtonCluster):
    """C4ButtonCluster, but each button also gets a dedicated binary_sensor.

    CONFIRMED gap found by the user: device_automation_triggers (fixed
    earlier — see control4_dimmer.py's module docstring) makes "top
    pressed"/"bottom pressed"/etc. selectable as an automation trigger,
    but there was no actual HA *entity* anywhere showing button activity
    — nothing in Developer Tools -> States, no history, nothing on a
    dashboard. zha_send_event alone only ever produces a bus event
    (zha_event), not an entity.

    CONFIRMED WRONG on real hardware, twice — see _make_dimmer_button_cluster's
    docstring for the full detail: first a bare EventableCluster (assumed,
    wrongly, to create an Event entity by itself), then a MultistateInput
    cluster with a custom ep_attribute (silently broke ZHA's cluster-
    handler discovery). Fixed by giving each virtual endpoint a real
    BinaryInput cluster with its default ep_attribute intact — confirmed
    correct against control4_z2io_zp.py's own working BinaryInput-backed
    binary_sensor. present_value now goes True on press and False once
    the press resolves (a simple click, or a hold ending), so each button
    gets a real binary_sensor entity whose on/off state reflects press
    and release, per the user's own suggested design.

    Also CONFIRMED WRONG on real hardware after the above: inheriting
    BUTTON_MAP from C4ButtonCluster (= DIMMER_BUTTON_MAP, written for the
    older c4.dmx.*-era on/off-button scheme where ids 0x00 AND 0x01 both
    meant "top" and 0x05 meant "bottom") silently routed every c4.dm.*
    press from BOTH physical buttons to "top" — id 1 (the real bottom
    button) resolved to "top" instead, and id 5 (needed for
    DIMMER_BUTTON_MAP's own "bottom") never appears in this protocol at
    all, so "bottom" never fired. Overridden with APD120_BUTTON_MAP
    (c4_helpers.py) — the real, confirmed 0=top/1=bottom scheme — instead
    of touching the shared DIMMER_BUTTON_MAP, which
    C4SwitchButtonCluster/C4DualOutletButtonCluster still rely on for
    their own, different physical devices.

    Overrides _fire_button_zha_event() (not _handle_button_event()) so
    the dimmer-specific on/off state sync in _sync_state_from_event /
    _sync_cc_event (unique to this class among C4ButtonCluster's
    subclasses — the scene controller has no load to sync) keeps
    running unchanged; only *where the zha_event fires* changes, from
    this cluster's own endpoint (197) to the matching virtual endpoint
    in DIMMER_BUTTON_EVENT_EP_MAP. device_automation_triggers
    (control4_dimmer.py) was updated to match — it now points at the
    virtual endpoints too, since events no longer fire on EP197 at all.
    """

    BUTTON_MAP = APD120_BUTTON_MAP

    def _fire_button_zha_event(self, action, button_id, button_name):
        ep_id = DIMMER_BUTTON_EVENT_EP_MAP.get(button_name)
        if ep_id is None:
            _LOGGER.warning(
                "C4 dimmer button: no virtual EP for button %r — add it "
                "to DIMMER_BUTTON_EVENT_EP_MAP", button_name,
            )
            return

        ep = self.endpoint.device.endpoints.get(ep_id)
        if ep is None:
            _LOGGER.warning(
                "C4 dimmer button: virtual EP %d not in device endpoints "
                "(re-pair after quirk update?)", ep_id,
            )
            return

        btn_cluster = ep.in_clusters.get(BinaryInput.cluster_id)
        if btn_cluster is None:
            _LOGGER.warning(
                "C4 dimmer button: no cluster 0x%04X on EP %d",
                BinaryInput.cluster_id, ep_id,
            )
            return

        btn_cluster.listener_event("zha_send_event", action, {ENDPOINT_ID: ep_id})
        if action == "press":
            btn_cluster.set_pressed(True)
        elif action in (
            SHORT_PRESS, DOUBLE_PRESS, TRIPLE_PRESS, QUADRUPLE_PRESS,
            LONG_RELEASE,
        ):
            # Any resolved click (single/double/triple/quadruple) or the
            # end of a hold means the press is over — LONG_PRESS itself is
            # excluded since it fires repeatedly WHILE still held, not on
            # release.
            btn_cluster.set_pressed(False)
        _LOGGER.debug(
            "C4 dimmer button: fired %r for %s on EP %d",
            action, button_name, ep_id,
        )


# ---------------------------------------------------------------------------
# Switch variant
# ---------------------------------------------------------------------------

class C4SwitchButtonCluster(C4ButtonCluster):
    """Button events for on/off switches — syncs OnOff only (no LevelControl)."""

    name         = "Control4 Switch Button Events"
    ep_attribute = "c4_switch_buttons"

    def _handle_dim_level_text(self, text):
        parts = text.split()
        try:
            level_raw = int(parts[-1], 16)
            _sync_ep1_onoff(self.endpoint.device, level_raw > 0, "sw_dm_t0c")
        except (ValueError, IndexError):
            _LOGGER.warning("C4: failed to parse dim level: '%s'", text)

    def _sync_cc_event(self, button_id, click_count):
        try:
            ep1 = self.endpoint.device.endpoints.get(1)
            if ep1 is None:
                return
            onoff_cluster = ep1.in_clusters.get(OnOff.cluster_id)
            if onoff_cluster is not None:
                if button_id == 0x01 and click_count >= 1:
                    _LOGGER.debug("C4 switch sync: ON (btn=0x01)")
                    onoff_cluster.update_attribute(
                        OnOff.AttributeDefs.on_off.id, True
                    )
                elif button_id == 0x05 and click_count >= 1:
                    _LOGGER.debug("C4 switch sync: OFF (btn=0x05)")
                    onoff_cluster.update_attribute(
                        OnOff.AttributeDefs.on_off.id, False
                    )
        except Exception:
            _LOGGER.warning("C4 switch sync: failed", exc_info=True)


class C4SwitchButtonClusterWithBinarySensor(C4SwitchButtonCluster):
    """C4SwitchButtonCluster, but each button also gets a dedicated binary_sensor.

    Mirrors C4DimmerButtonCluster's _fire_button_zha_event() override
    exactly (routes to DIMMER_BUTTON_EVENT_EP_MAP's virtual per-button
    endpoint instead of firing on this cluster's own endpoint 197) — added
    for the LSZ-101 to match the LDZ-101's per-button binary_sensor
    entities. BUTTON_MAP is inherited unchanged from C4SwitchButtonCluster
    (DIMMER_BUTTON_MAP: 0x00/0x01=top, 0x05=bottom — the switch's real
    c4.dmx.* protocol), unlike C4DimmerButtonCluster, which overrides it to
    APD120_BUTTON_MAP for the dimmer's different c4.dm.* protocol.
    """

    def _fire_button_zha_event(self, action, button_id, button_name):
        ep_id = DIMMER_BUTTON_EVENT_EP_MAP.get(button_name)
        if ep_id is None:
            _LOGGER.warning(
                "C4 switch button: no virtual EP for button %r — add it "
                "to DIMMER_BUTTON_EVENT_EP_MAP", button_name,
            )
            return

        ep = self.endpoint.device.endpoints.get(ep_id)
        if ep is None:
            _LOGGER.warning(
                "C4 switch button: virtual EP %d not in device endpoints "
                "(re-pair after quirk update?)", ep_id,
            )
            return

        btn_cluster = ep.in_clusters.get(BinaryInput.cluster_id)
        if btn_cluster is None:
            _LOGGER.warning(
                "C4 switch button: no cluster 0x%04X on EP %d",
                BinaryInput.cluster_id, ep_id,
            )
            return

        btn_cluster.listener_event("zha_send_event", action, {ENDPOINT_ID: ep_id})
        if action == "press":
            btn_cluster.set_pressed(True)
        elif action in (
            SHORT_PRESS, DOUBLE_PRESS, TRIPLE_PRESS, QUADRUPLE_PRESS,
            LONG_RELEASE,
        ):
            btn_cluster.set_pressed(False)
        _LOGGER.debug(
            "C4 switch button: fired %r for %s on EP %d",
            action, button_name, ep_id,
        )


# ---------------------------------------------------------------------------
# Dual outlet variant
# ---------------------------------------------------------------------------

class C4DualOutletButtonCluster(C4SwitchButtonCluster):
    """Button/state cluster for dual-outlet devices (LOZ-5S1-W).

    Protocol (from Wireshark captures):
      State announcements arrive as:
        0t<chan> sa c4.dm.tc <outlet_idx> <level>\r\n
      where outlet_idx is 00 or 01, level is 64 (ON) or 00 (OFF).

      The c4.dmx.* namespaces used by dimmers/switches are NOT used by
      the outlet — only c4.dm.tc (state announce) and c4.dm.tv (set).
    """

    name         = "Control4 Dual Outlet Button Events"
    ep_attribute = "c4_dual_outlet_buttons"

    def _sync_onoff_for_outlet(self, outlet_idx, is_on):
        ep_id = OUTLET_EP_MAP.get(outlet_idx)
        if ep_id is None:
            _LOGGER.warning("C4 dual outlet: unknown outlet index %d", outlet_idx)
            return
        try:
            ep = self.endpoint.device.endpoints.get(ep_id)
            if ep is None:
                return
            onoff = ep.in_clusters.get(OnOff.cluster_id)
            if onoff is not None:
                _LOGGER.debug(
                    "C4 dual outlet: ep%d (outlet %d) on_off=%s",
                    ep_id, outlet_idx, is_on,
                )
                onoff.update_attribute(OnOff.AttributeDefs.on_off.id, is_on)
        except Exception:
            _LOGGER.warning("C4 dual outlet: sync failed", exc_info=True)

    def _handle_state_announcement(self, namespace, data):
        """Handle c4.dm.tc state announcements from the outlet.

        Format: sa c4.dm.tc <outlet_idx_hex> <level_hex>
        outlet_idx: 00 or 01
        level: 64 (=100 decimal, ON) or 00 (OFF)
        """
        if namespace == "c4.dm.tc":
            if len(data) >= 2:
                try:
                    outlet_idx = int(data[0], 16)
                    level = int(data[1], 16)
                    is_on = level > 0
                    _LOGGER.debug(
                        "C4 dual outlet: c4.dm.tc outlet=%d level=%d on=%s",
                        outlet_idx, level, is_on,
                    )
                    self._sync_onoff_for_outlet(outlet_idx, is_on)
                except (ValueError, TypeError) as e:
                    _LOGGER.warning(
                        "C4 dual outlet: failed to parse c4.dm.tc: data=%s (%s)",
                        data, e,
                    )
            else:
                _LOGGER.warning(
                    "C4 dual outlet: c4.dm.tc too few fields: %s", data
                )
            return

        # Fall through to parent for any other namespaces (e.g. c4.dmx.*)
        super()._handle_state_announcement(namespace, data)

    def _sync_cc_event(self, button_id, click_count):
        # On dual outlet devices, cc button_id is the outlet index
        self._sync_onoff_for_outlet(button_id, click_count == 1)


# ---------------------------------------------------------------------------
# KPZ-6B1 keypad variant — per-button binary_sensor (press/release)
# ---------------------------------------------------------------------------

def _make_keypad_button_cluster(btn_id: int) -> type:
    """Return _make_binary_button_cluster's class for one KPZ-6B1 button.

    Mirrors _make_dimmer_button_cluster exactly (see
    _make_binary_button_cluster's docstring for the shared "two wrong
    turns" history). Physical Zigbee frames never arrive here — routing
    is done by C4KeypadButtonCluster._fire_button_zha_event().
    """
    return _make_binary_button_cluster(
        f"Button {btn_id + 1}",
        f"C4Keypad{btn_id}ButtonCluster",
    )


# One cluster class per keypad button — keyed by button id (0-5)
_KPZ6B1_BUTTON_CLUSTERS: dict[int, type] = {
    btn_id: _make_keypad_button_cluster(btn_id) for btn_id in KPZ6B1_BUTTON_MAP
}


class C4KeypadButtonCluster(C4ButtonCluster):
    """C4ButtonCluster for the KPZ-6B1 — each button gets a binary_sensor.

    Reuses the exact pattern already CONFIRMED working on the APD120/
    LDZ-101 dimmer: BUTTON_MAP + EVENT_MAP overridden for this device's
    own confirmed c4.kp.* protocol (see c4_helpers.py's KEYPAD_EVENT_MAP),
    and _fire_button_zha_event() overridden to route to the matching
    virtual per-button endpoint instead of firing on this cluster's own
    endpoint — present_value goes True on "press" (c4.kp.bb) and False
    once the press resolves (a simple click via c4.kp.cc, or a hold
    ending via c4.kp.be).
    """

    BUTTON_MAP = KPZ6B1_BUTTON_MAP
    EVENT_MAP  = KEYPAD_EVENT_MAP

    def handle_message(self, hdr, args):
        device = self.endpoint.device
        if not getattr(device, "_c4_kpz_managed_sent", False):
            device._c4_kpz_managed_sent = True
            c4_spawn(self._ensure_all_buttons_unmanaged())
        super().handle_message(hdr, args)

    async def _ensure_all_buttons_unmanaged(self):
        """Force all 6 buttons OUT of "Keypad Managed" and keep them that way.

        CONFIRMED c4.kp.llm <btn_single_hex_digit> <00|01> from six
        separate captured commands (one per button) toggling the
        "Keypad Managed" checkbox in Composer.

        CONFIRMED WRONG once (real hardware): this used to force
        managed=01, on the assumption that "Keypad Managed" meant "let
        an external controller manage this LED" — the natural reading
        given no Control4 controller is present in a plain ZHA install.
        A real Composer capture of the LED property panel disproved
        that: toggling "Keypad Managed" doesn't just change who's in
        charge, it changes what the two color properties MEAN — off,
        they're "On Color"/"Off Color" (a static color tied to a bound
        device's state); on, they become "Push Color"/"Release Color"
        (a momentary flash while held, reverting on release). Forcing
        managed=01 therefore put every button into the exact mode that
        reverts any color set via c4.kp.lv the instant it's pressed —
        this was self-inflicted, not inherent firmware behavior. Fixed
        by forcing managed=00 instead, which should keep whatever color
        was last set via lv/lo/lf static across physical presses.
        """
        device = self.endpoint.device
        all_sent = True
        for btn_id in KPZ6B1_BUTTON_MAP:
            seq = next_c4_seq(device)
            cmd = f"0s{seq:04x} c4.kp.llm {btn_id:x} 00"
            frame = _build_c4_frame(seq, cmd)
            _LOGGER.info(
                "C4 keypad (endpoint %d): forcing button %d to keypad-"
                "unmanaged — cmd: %s",
                self.endpoint.endpoint_id, btn_id, cmd,
            )
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
                all_sent = False
                _LOGGER.warning(
                    "C4 keypad (endpoint %d): failed to set button %d "
                    "keypad-unmanaged — %s",
                    self.endpoint.endpoint_id, btn_id, e,
                )
            await asyncio.sleep(C4_PROVISION_DELAY)
        if not all_sent:
            # e.g. radio not ready yet at boot — retry on the next message.
            device._c4_kpz_managed_sent = False

    def _fire_button_zha_event(self, action, button_id, button_name):
        ep_id = KPZ6B1_BUTTON_EP_MAP.get(button_id)
        if ep_id is None:
            _LOGGER.warning(
                "C4 keypad: no virtual EP for button %r — add it to "
                "KPZ6B1_BUTTON_EP_MAP", button_id,
            )
            return

        ep = self.endpoint.device.endpoints.get(ep_id)
        if ep is None:
            _LOGGER.warning(
                "C4 keypad: virtual EP %d not in device endpoints "
                "(re-pair after quirk update?)", ep_id,
            )
            return

        btn_cluster = ep.in_clusters.get(BinaryInput.cluster_id)
        if btn_cluster is None:
            _LOGGER.warning(
                "C4 keypad: no cluster 0x%04X on EP %d",
                BinaryInput.cluster_id, ep_id,
            )
            return

        btn_cluster.listener_event("zha_send_event", action, {ENDPOINT_ID: ep_id})
        if action == "press":
            btn_cluster.set_pressed(True)
        elif action in (
            SHORT_PRESS, DOUBLE_PRESS, TRIPLE_PRESS, QUADRUPLE_PRESS,
            LONG_RELEASE,
        ):
            btn_cluster.set_pressed(False)
        _LOGGER.debug(
            "C4 keypad button: fired %r for %s on EP %d",
            action, button_name, ep_id,
        )
