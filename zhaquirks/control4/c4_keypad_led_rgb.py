"""Per-button RGB LED-color light entities for the Control4 KPZ-6B1 keypad.

CONFIRMED from a real HC300 controller log: the KPZ-6B1's per-button LED
on-color is set via `0s<seq> c4.kp.lo <button_hex> <rrggbb_hex>` — unlike
the APD120/LDZ-101 dimmer (which bakes the button into the namespace
itself, e.g. c4.dm.l0o), the button index here is an explicit argument,
so one shared cluster class parameterized by button index covers all six
buttons instead of needing per-button namespace subclasses. The matching
off-color command (`c4.kp.lf <button_hex> <rrggbb_hex>`) and a per-button
"keypad managed" toggle (`c4.kp.llm <button_hex> <0|1>`, matching the
"Keypad Managed" checkbox seen in Composer) were also confirmed on the
wire but are not exposed here yet — only "on" color, matching the scope
of the equivalent dimmer feature.

Reuses C4LedOnOff/C4LedLevelControl from c4_led_rgb.py as-is (they are
already fully generic — endpoint-relative sibling lookups, no protocol-
specific namespace of their own) and subclasses C4LedColorCluster,
overriding only _send_color() to build the KPZ-6B1's own wire command
instead of the dimmer's. Everything else (XY-to-RGB conversion, the
LevelControl-brightness-as-Y bridge, the on()-resend race fix) is
inherited unchanged — see c4_led_rgb.py's own docstring for why each of
those exists.

Exported:
  _make_keypad_led_color_cluster() — factory for one button's on-color cluster
  _KEYPAD_LED_COLOR_CLUSTERS       — per-button virtual cluster dict (btn_id → class)
"""

import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from c4_helpers import C4_CLUSTER_ID, C4_PROFILE_BUTTON, KPZ6B1_BUTTON_MAP, _build_c4_frame, next_c4_seq
from c4_led_rgb import C4LedColorCluster

_LOGGER = logging.getLogger(__name__)


class C4KeypadLedColorCluster(C4LedColorCluster):
    """KPZ-6B1 per-button LED on-color — CONFIRMED c4.kp.lo <btn> <rgb>.

    Subclasses (via _make_keypad_led_color_cluster) set _BUTTON_IDX.
    """

    _BUTTON_IDX: int = 0

    async def _send_color(self, rgb_hex: str):
        """Send a `0s<seq> c4.kp.lo <btn> <rrggbb>` LED color command."""
        device = self.endpoint.device
        seq = next_c4_seq(device)
        cmd = f"0s{seq:04x} c4.kp.lo {self._BUTTON_IDX:02x} {rgb_hex}"

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
