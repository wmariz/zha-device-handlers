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

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

import zigpy.types as t
from zigpy.quirks import CustomCluster
from zigpy.zcl.foundation import BaseCommandDefs, ZCLCommandDef

from c4_helpers import C4_CLUSTER_ID, C4_PROFILE_BUTTON, KPZ6B1_BUTTON_MAP, _build_c4_frame, next_c4_seq
from c4_led_rgb import C4LedColorCluster

_LOGGER = logging.getLogger(__name__)

C4_KEYPAD_ALL_LED_CLUSTER_ID = 0xFC48


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

    async def _send_all(self, colors: list):
        """Send `0s<seq> c4.kp.lv ff ff <c1>..<c6>` in one wire frame."""
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

    async def set_all_colors(self, red, green, blue):
        """Set all 6 buttons to the SAME (red, green, blue) color."""
        color_hex = f"{int(red):02x}{int(green):02x}{int(blue):02x}"
        await self._send_all([color_hex] * 6)

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
        colors = [
            f"{int(r):02x}{int(g):02x}{int(b):02x}"
            for r, g, b in (
                (red_1, green_1, blue_1),
                (red_2, green_2, blue_2),
                (red_3, green_3, blue_3),
                (red_4, green_4, blue_4),
                (red_5, green_5, blue_5),
                (red_6, green_6, blue_6),
            )
        ]
        await self._send_all(colors)

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        """Log any unexpected inbound cluster requests."""
        _LOGGER.debug(
            "C4 keypad_all_led (endpoint %d): unexpected inbound request "
            "hdr=%s args=%s",
            self.endpoint.endpoint_id, hdr, args,
        )
