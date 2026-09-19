"""Per-button RGB LED-color light entities for the Control4 APD120/LDZ-101.

CONFIRMED from a real HC300 controller log: the LDZ-101's per-button LED
indicator color (shown while the dimmer is on) is set via
`0s<seq> c4.dm.l<button><o|f> <rrggbb_hex>` — `c4.dm.l0o` for the top
button's on-color was the only one actually observed transmitting on the
wire (the other three combos — l0f/l1o/l1f — were requested in Composer
but never sent, throttled by Composer's own internal debounce). This is
a different namespace family from c4_led_cluster.py's C4LEDCluster
(c4.dmx.led), which is the KC120277 scene-controller keypad's protocol
and is very likely non-functional for this device's own LEDs — hence a
separate, dedicated module rather than extending that one.

Per the user's request, only the "on" color is exposed (the LED is only
driven this way while led_attached is off — see c4_attached_switch.py —
otherwise the device's own built-in logic drives it), and the bottom
button's l1o is implemented by analogy with the confirmed top button
protocol (same shape, unconfirmed on real hardware — button index is
the only thing that differs from the one command actually captured).

CONFIRMED WRONG on real hardware: a first version gave each button a
virtual endpoint with just OnOff + Color, expecting ZHA's light
platform to claim the pair as one RGB light entity. It didn't — the
generic Switch platform claimed the bare OnOff cluster instead (two
unlabeled "Interruptor" entities appeared), and Color was left
completely unclaimed (no entity at all, not even a disabled one).
Fixed by adding a LevelControl cluster too, which is apparently what
ZHA's light-platform discovery actually keys on before treating an
endpoint as a light at all (Color then adds RGB support on top) — a
bare OnOff+Color pair, without LevelControl, is not enough.

Once LevelControl had to be added anyway, the user pointed out there's
no need for separate on/off-specific wire logic: Control4 already
treats black (000000) as "off" for these LEDs, so LevelControl's
brightness doubles as the brightness ("V"/"Y") component of whichever
color conversion is in play — 0% brightness naturally sends black, and
on()/off() just set brightness to 254/0 and resend the current color,
rather than carrying any bespoke on/off protocol logic of their own.

CONFIRMED WRONG on real hardware, a second time: with OnOff+Level+Color
present and color_capabilities declaring Hue_and_saturation, both
lights appeared with no color control at all — no error, just nothing.
Root-caused by reading zha's own light platform source: this zha
version's `_color_capabilities`/`_color_xy_supported` properties only
ever check for the `XY_attributes` capability bit, and its color-mode
branch is a plain `if color_temp: COLOR_TEMP else: XY` — there is no
Hue/Saturation branch anywhere in it. Declaring Hue_and_saturation
capability and implementing move_to_hue_and_saturation was building
against a color model this zha version's light platform simply never
consults. Rewritten to advertise XY_attributes and implement
move_to_color (CIE 1931 xy chromaticity + LevelControl's brightness as
Y), converting through the standard xyY -> linear sRGB -> gamma-
corrected sRGB pipeline (the same formula used by Philips Hue and
widely published) to get an rrggbb hex triplet for the wire command.

Exported:
  C4LedOnOff              — shared OnOff cluster for either LED endpoint
  C4LedLevelControl       — shared LevelControl cluster (brightness = "Y")
  C4TopLedColorCluster    — top LED color cluster    (c4.dm.l0o)
  C4BottomLedColorCluster — bottom LED color cluster (c4.dm.l1o, unconfirmed)
  LED_COLOR_EP_MAP        — {"top": ep_id, "bottom": ep_id}
"""

import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.quirks import CustomCluster
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import LevelControl, OnOff
from zigpy.zcl.clusters.lighting import Color
from zigpy.zcl.foundation import Status as ZCLStatus

from c4_helpers import C4_CLUSTER_ID, C4_PROFILE_BUTTON, _build_c4_frame, next_c4_seq

_LOGGER = logging.getLogger(__name__)

# top/bottom LED color light — one virtual endpoint each
LED_COLOR_EP_MAP = {"top": 202, "bottom": 203}


def _gamma_correct(c: float) -> int:
    """Linear sRGB component (0-1) -> gamma-corrected 0-255 byte."""
    c = max(0.0, min(1.0, c))
    if c <= 0.0031308:
        c = 12.92 * c
    else:
        c = 1.055 * (c ** (1.0 / 2.4)) - 0.055
    return max(0, min(255, round(c * 255)))


def _xy_to_rgb_hex(x_raw: int, y_raw: int, level_raw: int) -> str:
    """Convert ZCL CIE xy (0-65535 each) + level (0-254) to an rrggbb hex.

    Standard xyY -> linear sRGB -> gamma-corrected sRGB pipeline (the
    same formula published by Philips for their Hue bulbs). level_raw=0
    yields pure black — Control4's own "off" convention for these LEDs —
    so this one conversion covers both color and on/off, with no
    separate wire command needed for either.
    """
    x = x_raw / 65535.0
    y = y_raw / 65535.0
    brightness = min(max(level_raw / 254.0, 0.0), 1.0)

    if y <= 0 or brightness <= 0:
        return "000000"

    z = 1.0 - x - y
    big_y = brightness
    big_x = (big_y / y) * x
    big_z = (big_y / y) * z

    r = big_x * 3.2406 + big_y * -1.5372 + big_z * -0.4986
    g = big_x * -0.9689 + big_y * 1.8758 + big_z * 0.0415
    b = big_x * 0.0557 + big_y * -0.2040 + big_z * 1.0570

    return f"{_gamma_correct(r):02x}{_gamma_correct(g):02x}{_gamma_correct(b):02x}"


class C4LedColorCluster(CustomCluster, Color):
    """Color cluster: translates ZCL CIE xy (+ the sibling LevelControl's
    brightness, as Y) into the confirmed c4.dm.l<button>o <rrggbb> wire
    command.

    Subclasses set _C4_NAMESPACE (e.g. "c4.dm.l0o") and _C4_LABEL (for
    logging).
    """

    _CONSTANT_ATTRIBUTES = {
        Color.AttributeDefs.color_capabilities.id:
            Color.ColorCapabilities.XY_attributes,
    }

    _C4_NAMESPACE: str = ""
    _C4_LABEL: str = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A reasonable default xy (roughly white) so the entity has a
        # real starting color instead of "unknown" before the first
        # move_to_color.
        self._update_attribute(self.AttributeDefs.current_x.id, 21845)
        self._update_attribute(self.AttributeDefs.current_y.id, 21845)

    def _level(self) -> int:
        level_cluster = self.endpoint.in_clusters.get(LevelControl.cluster_id)
        if level_cluster is None:
            return 254
        return level_cluster.get(LevelControl.AttributeDefs.current_level.id, 254)

    async def command(
        self,
        command_id,
        *args,
        manufacturer=None,
        expect_reply=True,
        tsn=None,
        **kwargs,
    ):
        cmds = self.ServerCommandDefs
        if command_id != cmds.move_to_color.id:
            return await super().command(
                command_id, *args, manufacturer=manufacturer,
                expect_reply=expect_reply, tsn=tsn, **kwargs,
            )

        color_x, color_y = args[0], args[1]
        self._update_attribute(self.AttributeDefs.current_x.id, color_x)
        self._update_attribute(self.AttributeDefs.current_y.id, color_y)
        await self._send_current_color()
        return foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS

    async def _send_current_color(self):
        """Recompute RGB from the cached xy/level and send it."""
        color_x = self.get(self.AttributeDefs.current_x.id, 21845)
        color_y = self.get(self.AttributeDefs.current_y.id, 21845)
        await self._send_color(_xy_to_rgb_hex(color_x, color_y, self._level()))

    async def _send_color(self, rgb_hex: str):
        """Send a `0s<seq> <namespace> <rrggbb>` LED color command."""
        device = self.endpoint.device
        seq = next_c4_seq(device)
        cmd = f"0s{seq:04x} {self._C4_NAMESPACE} {rgb_hex}"

        _LOGGER.info(
            "C4 %s (endpoint %d): setting color=%s — cmd: %s",
            self._C4_LABEL, self.endpoint.endpoint_id, rgb_hex, cmd,
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
                "C4 %s (endpoint %d): failed to set color=%s — %s",
                self._C4_LABEL, self.endpoint.endpoint_id, rgb_hex, e,
            )


class C4TopLedColorCluster(C4LedColorCluster):
    """Top button LED color — CONFIRMED c4.dm.l0o wire command."""

    _C4_NAMESPACE = "c4.dm.l0o"
    _C4_LABEL = "top_led_color"


class C4BottomLedColorCluster(C4LedColorCluster):
    """Bottom button LED color — c4.dm.l1o by analogy, UNCONFIRMED."""

    _C4_NAMESPACE = "c4.dm.l1o"
    _C4_LABEL = "bottom_led_color"


class C4LedLevelControl(CustomCluster, LevelControl):
    """Brightness for an LED-color light — doubles as CIE "Y".

    Not forwarded to the device as its own command; changing it just
    recomputes and resends the sibling Color cluster's RGB value, since
    brightness=0 already yields black through _xy_to_rgb_hex.
    """

    cluster_id = LevelControl.cluster_id
    _SUCCESS = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._update_attribute(self.AttributeDefs.current_level.id, 254)

    def _color_cluster(self):
        return self.endpoint.in_clusters.get(Color.cluster_id)

    async def command(
        self,
        command_id,
        *args,
        manufacturer=None,
        expect_reply=False,
        tsn=None,
        **kwargs,
    ):
        cmds = self.ServerCommandDefs
        if command_id not in (
            cmds.move_to_level.id, cmds.move_to_level_with_on_off.id,
        ):
            return await super().command(
                command_id, *args, manufacturer=manufacturer,
                expect_reply=expect_reply, tsn=tsn, **kwargs,
            )

        level = args[0] if args else kwargs.get("level", 254)
        self._update_attribute(self.AttributeDefs.current_level.id, level)

        color = self._color_cluster()
        if color is not None:
            await color._send_current_color()

        return self._SUCCESS


class C4LedOnOff(CustomCluster, OnOff):
    """OnOff for an LED-color light: on/off just drive brightness to
    254/0 and resend the current color — brightness=0 already yields
    black on the wire, Control4's own "off" convention for these LEDs,
    so no separate on/off wire command is needed. Endpoint-relative
    lookup of the sibling clusters means this one class works for both
    the top and bottom virtual endpoints.
    """

    cluster_id = OnOff.cluster_id
    _SUCCESS = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._update_attribute(self.AttributeDefs.on_off.id, True)

    def _level_cluster(self):
        return self.endpoint.in_clusters.get(LevelControl.cluster_id)

    def _color_cluster(self):
        return self.endpoint.in_clusters.get(Color.cluster_id)

    async def command(
        self,
        command_id,
        *args,
        manufacturer=None,
        expect_reply=True,
        tsn=None,
        **kwargs,
    ):
        cmds = self.ServerCommandDefs
        if command_id == cmds.on.id:
            new_state = True
        elif command_id == cmds.off.id:
            new_state = False
        elif command_id == cmds.toggle.id:
            new_state = not bool(self.get(self.AttributeDefs.on_off.id, True))
        else:
            return await super().command(
                command_id, *args, manufacturer=manufacturer,
                expect_reply=expect_reply, tsn=tsn, **kwargs,
            )

        level = self._level_cluster()
        if level is not None:
            level._update_attribute(
                level.AttributeDefs.current_level.id, 254 if new_state else 0,
            )

        color = self._color_cluster()
        if color is not None:
            await color._send_current_color()

        self._update_attribute(self.AttributeDefs.on_off.id, new_state)
        return self._SUCCESS
