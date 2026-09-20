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
matters: a color set this way would revert to a stale previously-set
color the moment the physical button was pressed. Root cause: nothing
was updating the corresponding per-button C4KeypadLedColorCluster/
C4LedOnOff/C4LedLevelControl's cached state (current_x/current_y/
on_off/current_level), so it kept whatever the last individual-entity
color pick had been. _sync_button_entity() now pushes the newly-sent
color into those caches (via _rgb_to_xy_level(), the exact inverse of
c4_led_rgb._xy_to_rgb_hex) right after each successful send, so the HA
UI and any state resync both reflect the color that's actually on the
device.

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


def _rgb_to_xy_level(red: int, green: int, blue: int):
    """sRGB (0-255 each) -> (CIE x, CIE y, level), all in ZCL raw units.

    Exact inverse of c4_led_rgb._xy_to_rgb_hex's xyY -> linear sRGB ->
    gamma-corrected sRGB pipeline (same sRGB/D65 matrices, run backwards):
    gamma-expand each channel, convert to CIE XYZ, then to xy chromaticity
    + Y (brightness, reused as LevelControl's current_level, 0-254).
    """
    def _inv_gamma(c: int) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _inv_gamma(red), _inv_gamma(green), _inv_gamma(blue)
    x_lin = r * 0.4124 + g * 0.3576 + b * 0.1805
    y_lin = r * 0.2126 + g * 0.7152 + b * 0.0722
    z_lin = r * 0.0193 + g * 0.1192 + b * 0.9505

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
