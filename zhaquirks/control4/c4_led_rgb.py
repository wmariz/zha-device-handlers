"""Per-button RGB LED-color light entities for the Control4 APD120/LDZ-101.

CONFIRMED from two real HC300 controller logs: the LDZ-101's per-button
LED indicator color is set via `0s<seq> c4.dm.l<button><o|f>
<rrggbb_hex>` — all four combos now independently confirmed transmitting
and ACKed on the wire: `c4.dm.l0o`/`c4.dm.l0f` (top on/off-color) and
`c4.dm.l1o`/`c4.dm.l1f` (bottom on/off-color), the second capture using
5s delays between Composer script steps to avoid the throttling that
hid three of the four combos in the first capture. This is a different
namespace family from c4_led_cluster.py's C4LEDCluster (c4.dmx.led),
which is the KC120277 scene-controller keypad's protocol and is very
likely non-functional for this device's own LEDs — hence a separate,
dedicated module rather than extending that one.

Per the user's request, the "on" color is exposed first (the LED is
only driven this way while led_attached is off — see
c4_attached_switch.py — otherwise the device's own built-in logic
drives it), then the "off" color the same way once l0f/l1f were also
confirmed — four light entities total, one per (button, on/off) pair.

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

CONFIRMED WRONG on real hardware, a third time: picking a color sent
TWO wire commands ~150ms apart — the correct one, then a second one
reverting to the stale pre-pick color. HA issues an on/level command
alongside move_to_color for the same light.turn_on(rgb_color=...) call;
C4LedOnOff's own "resend the current color" side effect (needed so a
plain on/off toggle with no explicit color still does something) read
current_x/current_y before this cluster's own move_to_color handler had
updated them, racing it. Fixed with the same timestamp-suppression
technique already proven elsewhere in this device
(c4_helpers.c4_suppress_level_sync): C4LedColorCluster tracks when a
real move_to_color last landed, and C4LedOnOff's on() skips its resend
if one landed within the last second, since it already sent the right
value.

CONFIRMED via zha's own source (zha.application.platforms.light):
a light entity's Color-cluster attributes (current_x/current_y/
color_mode) are read back into the HA UI ONLY by a periodic poll —
every 45-75 MINUTES — never by an event listener like brightness/on-off
get. That poll always passes allow_cache=False (real safe_read()
call), meaning it normally issues a genuine over-the-air ZCL Read
Attributes request. This cluster has no real device-side ZCL backing
at all (color state is purely a local mirror this quirk maintains), so
without the read_attributes() override below, that periodic read would
just time out silently — and any code that updates current_x/current_y
some way other than through this cluster's own move_to_color() (e.g.
c4_keypad_led_rgb.C4KeypadAllLedCluster's set_all_colors/
set_individual_colors, which write straight to the wire and only poke
the cache afterwards) has no way to get the new color into the HA UI
promptly: nothing will show it until the next slow poll happens to
land, if ever. Fixed by forcing read_attributes() to always answer
from the local cache (never the wire) — safe, since there's nothing
real to read from anyway — which then lets external code force an
immediate, wire-free re-poll via the "homeassistant.update_entity"
service right after updating the cache.

Exported:
  C4LedOnOff                 — shared OnOff cluster for any LED endpoint
  C4LedLevelControl          — shared LevelControl cluster (brightness = "Y")
  C4TopLedColorCluster       — top LED on-color cluster     (c4.dm.l0o, CONFIRMED)
  C4BottomLedColorCluster    — bottom LED on-color cluster  (c4.dm.l1o, CONFIRMED)
  C4TopLedOffColorCluster    — top LED off-color cluster    (c4.dm.l0f, CONFIRMED)
  C4BottomLedOffColorCluster — bottom LED off-color cluster (c4.dm.l1f, CONFIRMED)
  LED_COLOR_EP_MAP           — {"top": ep_id, "bottom": ep_id} (on-color)
  LED_OFF_COLOR_EP_MAP       — {"top": ep_id, "bottom": ep_id} (off-color)
"""

import logging
import os
import sys
import time

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

# top/bottom LED on-color light — one virtual endpoint each
LED_COLOR_EP_MAP = {"top": 202, "bottom": 203}

# top/bottom LED off-color light — one virtual endpoint each
LED_OFF_COLOR_EP_MAP = {"top": 204, "bottom": 205}


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
        Color.AttributeDefs.color_mode.id:
            Color.ColorMode.X_and_Y,
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
        self._last_move_to_color_time = 0.0

    async def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None,
    ):
        """Always answer from the local cache — see this module's
        docstring for why a real over-the-air read would just time out,
        and why that matters for ZHA's slow periodic color-attribute
        poll.
        """
        return await super().read_attributes(
            attributes, allow_cache=True, only_cache=True, manufacturer=manufacturer,
        )

    def _level(self) -> int:
        level_cluster = self.endpoint.in_clusters.get(LevelControl.cluster_id)
        if level_cluster is None:
            return 254
        return level_cluster.get(LevelControl.AttributeDefs.current_level.id, 254)

    def recently_set_explicitly(self, window: float = 1.0) -> bool:
        """True if a real move_to_color landed within the last `window`s.

        CONFIRMED on real hardware: picking a color sends two wire
        commands — the correct one from move_to_color, then a second one
        ~150ms later with the stale pre-pick color. HA's light platform
        issues an on/level command alongside move_to_color for the same
        light.turn_on(rgb_color=...) call, and C4LedOnOff/
        C4LedLevelControl's own "resend the current color" side effect
        (needed for a plain on/off toggle with no explicit color) races
        it — it reads current_x/current_y before this cluster's own
        move_to_color handler has updated them, sending the old value
        right after the correct one. Callers check this before resending
        so a real, very recent color pick always wins.
        """
        return (time.monotonic() - self._last_move_to_color_time) < window

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

        # CONFIRMED on real hardware: ZHA's own light platform calls this
        # cluster's auto-generated move_to_color() wrapper with color_x/
        # color_y as KEYWORD arguments, not positional — args was empty,
        # so args[0]/args[1] raised IndexError before ever reaching
        # _send_current_color(), which is why the color never made it to
        # the wire despite the UI looking correct. Fall back to kwargs.
        color_x = args[0] if len(args) > 0 else kwargs.get("color_x")
        color_y = args[1] if len(args) > 1 else kwargs.get("color_y")
        if color_x is None:
            color_x = self.get(self.AttributeDefs.current_x.id, 21845)
        if color_y is None:
            color_y = self.get(self.AttributeDefs.current_y.id, 21845)
        self._update_attribute(self.AttributeDefs.current_x.id, color_x)
        self._update_attribute(self.AttributeDefs.current_y.id, color_y)
        self._last_move_to_color_time = time.monotonic()
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
    """Bottom button LED color — CONFIRMED c4.dm.l1o wire command."""

    _C4_NAMESPACE = "c4.dm.l1o"
    _C4_LABEL = "bottom_led_color"


class C4TopLedOffColorCluster(C4LedColorCluster):
    """Top button LED off-color — CONFIRMED c4.dm.l0f wire command."""

    _C4_NAMESPACE = "c4.dm.l0f"
    _C4_LABEL = "top_led_off_color"


class C4BottomLedOffColorCluster(C4LedColorCluster):
    """Bottom button LED off-color — CONFIRMED c4.dm.l1f wire command."""

    _C4_NAMESPACE = "c4.dm.l1f"
    _C4_LABEL = "bottom_led_off_color"


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
            # CONFIRMED on real hardware: HA issues an on/level command
            # alongside move_to_color for the same light.turn_on(rgb_
            # color=...) call. Resending here unconditionally raced that
            # real pick — this handler read current_x/current_y before
            # move_to_color's own handler had updated them, sending the
            # stale pre-pick color ~150ms after the correct one. Skip
            # the resend when a real color command landed moments ago;
            # it already sent the right value.
            if new_state and color.recently_set_explicitly():
                _LOGGER.debug(
                    "C4 %s: skipping on() color resend — a real "
                    "move_to_color landed moments ago", color._C4_LABEL,
                )
            else:
                await color._send_current_color()

        self._update_attribute(self.AttributeDefs.on_off.id, new_state)
        return self._SUCCESS
