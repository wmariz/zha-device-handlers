"""Per-button RGB LED-color light entities for the Control4 KPZ-6B1 keypad.

CONFIRMED from TWO real HC300 controller logs. The first log showed
`c4.kp.lo`/`c4.kp.lf` (on-color/off-color) being ACKed on the wire, and an
initial version of this module used `lo` for the light entity below. A
SECOND log — captured from a genuine Composer script ("Set all LED current
colors to X", "Set LED: N current color to Y") — revealed the command
Control4's own scripts actually use is `c4.kp.lv` ("current" color): it
sets a button's displayed color immediately, independent of on/off state
(confirmed via the ButtonStatus XML's <LEDCurColor> field tracking it
exactly, regardless of <CurState>). Since `lv` is strictly more useful
(bypasses on/off state entirely — matches how these buttons are actually
driven in practice) and the user confirmed `lo`/`lf` are not needed, this
module now exposes only `lv`, in two forms:

  - Single button:  `c4.kp.lv <btn_2digit_hex> <rrggbb>`
  - All 6 at once:   `c4.kp.lv ff ff <c1> <c2> <c3> <c4> <c5> <c6>`
    (a literal "ff ff" sentinel pair, then 6 RGB values, one per button —
    confirmed from the same Composer "SET_ALL_LED_COLOR" capture; every
    captured all-at-once command sent the SAME color 6 times, matching
    the Composer action's own wording, "Set all LED current colors to
    <color>" — a single color for every button, not 6 independent ones).

The all-at-once form is exposed as its own manufacturer-specific cluster
(C4KeypadAllLedCluster, cluster_id 0xFC48 — the next free C4 cluster ID;
see the registry comment in c4_helpers.py) rather than as a 6th light
attribute, since it is materially faster than 6 sequential single-button
writes (one wire frame instead of six) and has no natural per-entity
home. It exposes two commands over the same wire format:
  - set_all_colors(red, green, blue)         — one triplet, repeated
    across all 6 slots (matches every captured example — Composer's own
    "Set all LED current colors to <color>" sends the same color 6x).
  - set_individual_colors(red_1, green_1, blue_1, ..., red_6, green_6,
    blue_6) — 6 independent triplets, for the case where each button
    should get its own color in a single frame instead of one per
    button via the 6 single-button light entities.

Both commands bypass each button's own light entity clusters entirely —
they write straight to the wire. A real-hardware report showed why that
matters: after using either command, an affected light entity's HA card
still showed its old color (or, briefly, the correct brightness/on-state
but the wrong color — see _rgb_to_xy_level()'s own docstring for a first,
insufficient fix attempt around the xy<->rgb matrix). _sync_button_entity()
pushes the newly-sent color into the per-button C4KeypadLedColorCluster/
C4LedOnOff/C4LedLevelControl caches (current_x/current_y/on_off/
current_level) right after each successful send — necessary but NOT
sufficient by itself: CONFIRMED via zha's own source, its light platform
only re-reads Color-cluster attributes on a 45-75 MINUTE periodic poll
(brightness/on-off update instantly via event listeners; color does not).
_force_light_refresh() (below) closes that gap by asking HA to run that
poll immediately via the "homeassistant.update_entity" service, which is
now safe and wire-free thanks to C4LedColorCluster.read_attributes()
always answering from cache (see c4_led_rgb.py's docstring).

The physical LED's color was never wrong in any of this — set_all_colors/
set_individual_colors send the user's exact RGB bytes with zero
conversion, no matter what. Every fix described above is purely about
getting the HA UI's displayed color to catch up to what's already on
the device.

Per-button light entities reuse C4LedOnOff/C4LedLevelControl from
c4_led_rgb.py as-is (they are already fully generic — endpoint-relative
sibling lookups, no protocol-specific namespace of their own) and
subclass C4LedColorCluster, overriding only _send_color() to build the
KPZ-6B1's own wire command. Everything else (XY-to-RGB conversion, the
LevelControl-brightness-as-Y bridge, the on()-resend race fix) is
inherited unchanged — see c4_led_rgb.py's own docstring for why each of
those exists.

Exported:
  _make_keypad_led_color_cluster() — factory for one button's color cluster
  _KEYPAD_LED_COLOR_CLUSTERS       — per-button virtual cluster dict (btn_id → class)
  C4KeypadAllLedCluster            — manufacturer-specific "set all 6 at once" cluster
                                      (set_all_colors / set_individual_colors)
"""

import asyncio
import logging
import os
import sys
import time

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

import zigpy.types as t
from zigpy.quirks import CustomCluster
from zigpy.zcl.clusters.general import LevelControl, OnOff
from zigpy.zcl.clusters.lighting import Color
from zigpy.zcl.foundation import BaseCommandDefs, ZCLCommandDef

from c4_helpers import (
    C4_CLUSTER_ID, C4_PROFILE_BUTTON, KPZ6B1_BUTTON_MAP, KPZ6B1_LED_EP_MAP,
    _build_c4_frame, next_c4_seq,
)
from c4_led_rgb import C4LedColorCluster

_LOGGER = logging.getLogger(__name__)

C4_KEYPAD_ALL_LED_CLUSTER_ID = 0xFC48


def _get_hass(device):
    """Same lookup as control4_z2io_zp.py's own _get_hass — duplicated
    locally rather than imported, per this project's established rule
    that small stateless helpers are safe to duplicate across quirk
    modules (see feedback_zigpy_cluster_custom_attrs memory).
    """
    app = device.application
    for attr in ('hass', '_hass'):
        h = getattr(app, attr, None)
        if h is not None:
            return h
    gw = getattr(app, 'zha_gateway', None)
    if gw is not None:
        return getattr(gw, 'hass', None)
    return None


async def _force_light_refresh(device, ep_id: int):
    """Ask HA to immediately re-poll the light entity at endpoint `ep_id`.

    See c4_led_rgb.py's module docstring: ZHA's light platform only
    re-reads Color-cluster attributes on a periodic 45-75 MINUTE poll,
    and C4LedColorCluster.read_attributes() now always answers that
    poll from the local cache (never the wire). Forcing that poll here,
    right after set_all_colors/set_individual_colors update the cache,
    is what makes the HA UI reflect the new color immediately instead
    of up to an hour later.
    """
    hass = _get_hass(device)
    if hass is None:
        return
    try:
        from homeassistant.helpers import entity_registry as er
        unique_id = f"{device.ieee}-{ep_id}"
        entity_id = er.async_get(hass).async_get_entity_id("light", "zha", unique_id)
        if entity_id is None:
            _LOGGER.debug(
                "C4 keypad_all_led: no light entity found for unique_id=%s",
                unique_id,
            )
            return
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": entity_id},
        )
    except Exception as e:
        _LOGGER.debug(
            "C4 keypad_all_led: failed to force-refresh endpoint %d — %s",
            ep_id, e,
        )


def _rgb_to_xy_level(red: int, green: int, blue: int):
    """sRGB (0-255 each) -> (CIE x, CIE y, level), all in ZCL raw units.

    CONFIRMED WRONG once: an earlier version of this function used the
    standard/narrow sRGB D65 XYZ matrix — the exact inverse of
    c4_led_rgb._xy_to_rgb_hex's own matrix, which is what actually goes
    out on the wire (so the physical LED was never affected). But Home
    Assistant's own light platform renders an xy-mode entity's displayed
    color using ITS OWN xy<->rgb conversion (homeassistant.util.color),
    which uses a DIFFERENT matrix — the "Wide RGB D65" formula also used
    by the Philips Hue SDK — not the narrow sRGB one. Feeding that
    function's inverse's output back through HA's own (different)
    forward formula produced a visibly wrong color in the HA UI (the
    physical device was always correct, only the on-screen card was
    off). Fixed by matching HA's own matrix exactly here, so the value
    written to current_x/current_y round-trips correctly through HA's
    own renderer.
    """
    def _gamma_expand(c: int) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _gamma_expand(red), _gamma_expand(green), _gamma_expand(blue)

    # Wide RGB D65 conversion formula — matches Home Assistant's
    # color_RGB_to_xy_brightness() (homeassistant/util/color.py) exactly.
    x_lin = r * 0.664511 + g * 0.154324 + b * 0.162028
    y_lin = r * 0.283881 + g * 0.668433 + b * 0.047685
    z_lin = r * 0.000088 + g * 0.072310 + b * 0.986039

    total = x_lin + y_lin + z_lin
    if total <= 0:
        x, y = 0.3127, 0.3290  # D65 white point fallback (black input)
    else:
        x, y = x_lin / total, y_lin / total

    level = round(max(0.0, min(1.0, y_lin)) * 254)
    return round(x * 65535), round(y * 65535), level


class C4KeypadLedColorCluster(C4LedColorCluster):
    """KPZ-6B1 per-button LED current-color — CONFIRMED c4.kp.lv <btn> <rgb>.

    Subclasses (via _make_keypad_led_color_cluster) set _BUTTON_IDX.
    """

    _BUTTON_IDX: int = 0

    async def _send_color(self, rgb_hex: str):
        """Send a `0s<seq> c4.kp.lv <btn> <rrggbb>` LED color command."""
        device = self.endpoint.device
        seq = next_c4_seq(device)
        cmd = f"0s{seq:04x} c4.kp.lv {self._BUTTON_IDX:02x} {rgb_hex}"

        _LOGGER.info(
            "C4 keypad_led_color button %d (endpoint %d): setting color=%s "
            "— cmd: %s",
            self._BUTTON_IDX, self.endpoint.endpoint_id, rgb_hex, cmd,
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
                "C4 keypad_led_color button %d (endpoint %d): failed to "
                "set color=%s — %s",
                self._BUTTON_IDX, self.endpoint.endpoint_id, rgb_hex, e,
            )


def _make_keypad_led_color_cluster(btn_id: int) -> type:
    """Return a unique C4KeypadLedColorCluster subclass for one button."""

    class _Cluster(C4KeypadLedColorCluster):
        _BUTTON_IDX = btn_id

    _Cluster.__name__     = f"C4Keypad{btn_id}LedColorCluster"
    _Cluster.__qualname__ = _Cluster.__name__
    return _Cluster


# One color cluster class per keypad button — keyed by button id (0-5)
_KEYPAD_LED_COLOR_CLUSTERS: dict[int, type] = {
    btn_id: _make_keypad_led_color_cluster(btn_id) for btn_id in KPZ6B1_BUTTON_MAP
}


class C4KeypadAllLedCluster(CustomCluster):
    """Set all 6 KPZ-6B1 button LED current-colors in a single wire frame.

    CONFIRMED c4.kp.lv ff ff <c1> <c2> <c3> <c4> <c5> <c6> from a real
    Composer "SET_ALL_LED_COLOR" script capture — faster than 6 sequential
    single-button `lv` writes since HA/ZHA scripts (e.g. a "set all leds"
    button) only need one service call instead of six. Every captured
    example used the SAME color 6 times (set_all_colors below), but the
    wire format itself carries 6 independent RGB slots, so a second
    command (set_individual_colors) is also exposed for the case where
    the 6 buttons should each get their own color in one frame.

    Usage from Home Assistant (via zha.issue_zigbee_cluster_command):
      service: zha.issue_zigbee_cluster_command
      data:
        ieee: "00:0f:ff:..."
        endpoint_id: 197
        cluster_id: 0xFC48
        cluster_type: in
        command: 0          # set_all_colors
        command_type: server
        args:
          - 255   # red
          - 0     # green
          - 0     # blue
    """

    cluster_id = C4_KEYPAD_ALL_LED_CLUSTER_ID
    name = "Control4 Keypad All-LED Control"
    ep_attribute = "c4_keypad_all_led"
    _c4_custom_handler = True

    class ServerCommandDefs(BaseCommandDefs):
        """Server commands exposed to ZHA UI and service calls."""

        set_all_colors = ZCLCommandDef(
            id=0x00,
            schema={
                "red": t.uint8_t,
                "green": t.uint8_t,
                "blue": t.uint8_t,
            },
            is_manufacturer_specific=True,
        )

        set_individual_colors = ZCLCommandDef(
            id=0x01,
            schema={
                "red_1": t.uint8_t, "green_1": t.uint8_t, "blue_1": t.uint8_t,
                "red_2": t.uint8_t, "green_2": t.uint8_t, "blue_2": t.uint8_t,
                "red_3": t.uint8_t, "green_3": t.uint8_t, "blue_3": t.uint8_t,
                "red_4": t.uint8_t, "green_4": t.uint8_t, "blue_4": t.uint8_t,
                "red_5": t.uint8_t, "green_5": t.uint8_t, "blue_5": t.uint8_t,
                "red_6": t.uint8_t, "green_6": t.uint8_t, "blue_6": t.uint8_t,
            },
            is_manufacturer_specific=True,
        )

    def _sync_button_entity(self, btn_id: int, red: int, green: int, blue: int):
        """Update button `btn_id`'s light entity cache to match `red/green/blue`.

        set_all_colors/set_individual_colors send the raw c4.kp.lv wire
        command directly, bypassing each button's own C4KeypadLedColorCluster/
        C4LedOnOff/C4LedLevelControl (endpoints 210-215 — see
        c4_led_rgb.py). Left alone, those clusters' cached current_x/
        current_y/on_off/current_level stay whatever they were last set
        to through the individual per-button light entity, which is both
        visibly wrong in the HA UI (light card shows the old color) and
        the likely source of a CONFIRMED bug: a stale cached color
        reappeared on the physical button's LED right after a press,
        overwriting the color this cluster had just set — closing this
        gap keeps every cache consistent with the last color actually
        sent to the device.
        """
        ep = self.endpoint.device.endpoints.get(KPZ6B1_LED_EP_MAP.get(btn_id))
        if ep is None:
            return

        x_raw, y_raw, level = _rgb_to_xy_level(red, green, blue)

        color = ep.in_clusters.get(Color.cluster_id)
        if color is not None:
            color._update_attribute(Color.AttributeDefs.current_x.id, x_raw)
            color._update_attribute(Color.AttributeDefs.current_y.id, y_raw)
            color._last_move_to_color_time = time.monotonic()

        level_cluster = ep.in_clusters.get(LevelControl.cluster_id)
        if level_cluster is not None:
            level_cluster._update_attribute(
                LevelControl.AttributeDefs.current_level.id, level,
            )

        onoff = ep.in_clusters.get(OnOff.cluster_id)
        if onoff is not None:
            onoff._update_attribute(OnOff.AttributeDefs.on_off.id, level > 0)

    async def _send_all(self, rgb_values: list):
        """Send `0s<seq> c4.kp.lv ff ff <c1>..<c6>` in one wire frame.

        `rgb_values` is a list of 6 (red, green, blue) int triplets, one
        per button, in order.
        """
        colors = [f"{r:02x}{g:02x}{b:02x}" for r, g, b in rgb_values]

        device = self.endpoint.device
        seq = next_c4_seq(device)
        cmd = f"0s{seq:04x} c4.kp.lv ff ff " + " ".join(colors)

        _LOGGER.info(
            "C4 keypad_all_led (endpoint %d): setting all buttons to "
            "colors=%s — cmd: %s",
            self.endpoint.endpoint_id, colors, cmd,
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
                "C4 keypad_all_led (endpoint %d): failed to set all "
                "buttons to colors=%s — %s",
                self.endpoint.endpoint_id, colors, e,
            )
            return

        for btn_id, (r, g, b) in zip(KPZ6B1_BUTTON_MAP, rgb_values):
            self._sync_button_entity(btn_id, r, g, b)
            ep_id = KPZ6B1_LED_EP_MAP.get(btn_id)
            if ep_id is not None:
                asyncio.ensure_future(_force_light_refresh(device, ep_id))

    async def set_all_colors(self, red, green, blue):
        """Set all 6 buttons to the SAME (red, green, blue) color."""
        rgb = (int(red), int(green), int(blue))
        await self._send_all([rgb] * 6)

    async def set_individual_colors(
        self,
        red_1, green_1, blue_1,
        red_2, green_2, blue_2,
        red_3, green_3, blue_3,
        red_4, green_4, blue_4,
        red_5, green_5, blue_5,
        red_6, green_6, blue_6,
    ):
        """Set each of the 6 buttons to its own (red, green, blue) color."""
        rgb_values = [
            (int(r), int(g), int(b))
            for r, g, b in (
                (red_1, green_1, blue_1),
                (red_2, green_2, blue_2),
                (red_3, green_3, blue_3),
                (red_4, green_4, blue_4),
                (red_5, green_5, blue_5),
                (red_6, green_6, blue_6),
            )
        ]
        await self._send_all(rgb_values)

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        """Log any unexpected inbound cluster requests."""
        _LOGGER.debug(
            "C4 keypad_all_led (endpoint %d): unexpected inbound request "
            "hdr=%s args=%s",
            self.endpoint.endpoint_id, hdr, args,
        )
