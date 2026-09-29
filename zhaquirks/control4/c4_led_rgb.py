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
updated them, racing it. Fixed with a timestamp-suppression window:
C4LedColorCluster tracks when a
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

That same async_update() reads OnOff, LevelControl, and Color
SEQUENTIALLY in one call, and none of the three clusters on this
endpoint has any real device-side ZCL backing — so C4LedOnOff and
C4LedLevelControl need the exact same read_attributes() override, or
their own real-wire reads would stall/fail before the call chain ever
reaches the (already-fixed) Color read.

Exported:
  C4LedOnOff                 — shared OnOff cluster for any LED endpoint
  C4LedLevelControl          — shared LevelControl cluster (brightness = "Y")
  C4TopLedColorCluster       — top LED on-color cluster     (c4.dm.l0o, CONFIRMED)
  C4BottomLedColorCluster    — bottom LED on-color cluster  (c4.dm.l1o, CONFIRMED)
  C4TopLedOffColorCluster    — top LED off-color cluster    (c4.dm.l0f, CONFIRMED)
  C4BottomLedOffColorCluster — bottom LED off-color cluster (c4.dm.l1f, CONFIRMED)
  LED_COLOR_EP_MAP           — {"top": ep_id, "bottom": ep_id} (on-color)
  LED_OFF_COLOR_EP_MAP       — {"top": ep_id, "bottom": ep_id} (off-color)
  C4DimmerAllLedCluster      — exact-RGB "set all LEDs" command (EP197, 0xFC48)
  rgb_to_xy_level / sync_led_entity — shared with c4_keypad_led_rgb.py
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
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import LevelControl, OnOff
from zigpy.zcl.clusters.lighting import Color
from zigpy.zcl.foundation import BaseCommandDefs, ZCLCommandDef
from zigpy.zcl.foundation import Status as ZCLStatus

from c4_helpers import C4_CLUSTER_ID, C4_PROFILE_BUTTON, _build_c4_frame, next_c4_seq

_LOGGER = logging.getLogger(__name__)

# top/bottom LED on-color light — one virtual endpoint each
LED_COLOR_EP_MAP = {"top": 202, "bottom": 203}

# top/bottom LED off-color light — one virtual endpoint each
LED_OFF_COLOR_EP_MAP = {"top": 204, "bottom": 205}

# "Set all LEDs" cluster (same ID on the dimmer/switch and the KPZ-6B1).
C4_ALL_LED_CLUSTER_ID = 0xFC48

# Bit order of C4DimmerAllLedCluster.set_all_colors' `leds` mask.
DIMMER_ALL_LED_MASK_EPS = (
    LED_COLOR_EP_MAP["top"],         # bit 0 — top on-color
    LED_COLOR_EP_MAP["bottom"],      # bit 1 — bottom on-color
    LED_OFF_COLOR_EP_MAP["top"],     # bit 2 — top off-color
    LED_OFF_COLOR_EP_MAP["bottom"],  # bit 3 — bottom off-color
)


# Both conversions below mirror Home Assistant's own (homeassistant/util/
# color.py: color_RGB_to_xy_brightness / color_xy_brightness_to_RGB), which
# use the "Wide RGB D65" matrices from the Philips Hue SDK. HA converts a
# light.turn_on(rgb_color=...) to xy with the forward matrix before this
# quirk ever sees it, and renders current_x/current_y back to RGB for its UI
# with the inverse — using anything else here shifts hues (e.g. ff8000 came
# out as ff9400 with the plain sRGB matrix this used before).

def rgb_to_xy_level(red: int, green: int, blue: int):
    """sRGB (0-255 each) -> (CIE x, CIE y, level), all in ZCL raw units.

    level is the color's own luminance (Y) on the 0-254 LevelControl scale,
    so a dark color gets a low level. Used to keep a LED light entity's
    cache in sync after a raw-RGB set_all_colors write.
    """
    def _gamma_expand(c: int) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _gamma_expand(red), _gamma_expand(green), _gamma_expand(blue)

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


def _xy_to_rgb_hex(x_raw: int, y_raw: int, level_raw: int) -> str:
    """Convert ZCL CIE xy (0-65535 each) + level (0-254) to an rrggbb hex.

    Inverse of rgb_to_xy_level(): xyY -> linear RGB -> gamma-corrected
    sRGB, then (like HA) negatives clamped to 0 and, if any channel
    exceeds 1, all three scaled down by it so the hue is preserved rather
    than clipped. level_raw=0 yields pure black — Control4's own "off"
    convention for these LEDs — so this one conversion covers both color
    and on/off, with no separate wire command needed for either.
    """
    x = x_raw / 65535.0
    y = y_raw / 65535.0
    big_y = min(max(level_raw / 254.0, 0.0), 1.0)

    if y <= 0 or big_y <= 0:
        return "000000"

    big_x = (big_y / y) * x
    big_z = (big_y / y) * (1.0 - x - y)

    r = big_x * 1.656492 - big_y * 0.354851 - big_z * 0.255038
    g = -big_x * 0.707196 + big_y * 1.655397 + big_z * 0.036152
    b = big_x * 0.051713 - big_y * 0.121364 + big_z * 1.011530

    def _gamma_compress(c: float) -> float:
        return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055

    r, g, b = (max(0.0, _gamma_compress(c)) for c in (r, g, b))
    peak = max(r, g, b)
    if peak > 1.0:
        r, g, b = r / peak, g / peak, b / peak

    return "".join(f"{min(255, round(c * 255)):02x}" for c in (r, g, b))


def sync_led_entity(ep, red: int, green: int, blue: int) -> None:
    """Update a LED light endpoint's cached color/brightness/on-off to match
    a raw RGB just written to the wire by a set_all_colors-style command.

    Those commands bypass the light entity's own clusters, so without this
    the entity keeps showing (and, on its next on()/brightness change,
    resends) whatever color was last set through the entity itself. HA's
    light platform only re-reads current_x/current_y on its periodic poll,
    so callers (the HA scripts) follow up with homeassistant.update_entity.
    """
    x_raw, y_raw, level = rgb_to_xy_level(red, green, blue)

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


class _C4LocalOnlyReadMixin:
    """read_attributes() that never touches the wire and never delegates
    to the base Cluster implementation's own allow_cache/only_cache/
    _CONSTANT_ATTRIBUTES machinery — CONFIRMED WRONG on real hardware:
    a _CONSTANT_ATTRIBUTES-declared color_mode never actually came back
    as resolved through `super().read_attributes(..., only_cache=True)`
    (zigpy's own debug log kept showing it in the "still needs a real
    read, skipping" bucket, cycle after cycle, across multiple fix
    attempts), for reasons not cleanly traceable without the exact
    installed zigpy/zhaquirks version's source open in front of us.

    None of C4LedOnOff/C4LedLevelControl/C4LedColorCluster have any real
    device-side ZCL backing at all — every attribute they expose is
    either a constant or a local mirror this quirk maintains via
    _update_attribute() — so instead of trying to thread that value
    through the base class's cache logic, this answers every read
    directly from self.get(), the same already-proven mechanism
    C4LedColorCluster._level() has used successfully throughout this
    entire project to read a sibling cluster's cached value. Mirrors
    c4_basic_cluster.py's C4BasicCluster.read_attributes() in preserving
    each requested attribute's original key type (str name or int id) —
    ZHA's own light platform reads by NAME (see this module's docstring).
    """

    async def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None,
    ):
        success: dict = {}
        failure: dict = {}
        for attr in attributes:
            try:
                attr_def = self.find_attribute(attr)
            except KeyError:
                failure[attr] = foundation.Status.UNSUPPORTED_ATTRIBUTE
                continue
            value = self.get(attr_def.id, None)
            if value is not None:
                success[attr] = value
            else:
                failure[attr] = foundation.Status.UNSUPPORTED_ATTRIBUTE
        return success, failure


class C4LedColorCluster(_C4LocalOnlyReadMixin, CustomCluster, Color):
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
    # Icon for the light entity built on this cluster — applied by c4_hooks'
    # Patch 6, since quirk entity metadata has no icon field.
    _c4_led_icon: str = "mdi:led-outline"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A reasonable default xy (roughly white) so the entity has a
        # real starting color instead of "unknown" before the first
        # move_to_color. color_mode is also seeded directly (belt and
        # suspenders alongside the _CONSTANT_ATTRIBUTES declaration
        # above) since self.get() checks _CONSTANT_ATTRIBUTES first
        # anyway — see _C4LocalOnlyReadMixin.
        self._update_attribute(self.AttributeDefs.current_x.id, 21845)
        self._update_attribute(self.AttributeDefs.current_y.id, 21845)
        self._update_attribute(
            self.AttributeDefs.color_mode.id, Color.ColorMode.X_and_Y,
        )
        self._last_move_to_color_time = 0.0

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

    async def _send_color(self, rgb_hex: str) -> bool:
        """Send a `0s<seq> <namespace> <rrggbb>` LED color command.

        Returns True if the frame was handed to the radio.
        """
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
            return False
        return True


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


class C4LedLevelControl(_C4LocalOnlyReadMixin, CustomCluster, LevelControl):
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


class C4LedOnOff(_C4LocalOnlyReadMixin, CustomCluster, OnOff):
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


def _zha_device_of(cluster):
    """The zha library Device wrapping `cluster`'s device, or None.

    zha attaches an event forwarder as a listener to every cluster on a
    ZHA-profile endpoint; it holds the zha Endpoint, whose .device lists
    the device's platform entities. Private structure, so any mismatch
    just returns None.
    """
    for listener, _ in list(getattr(cluster, "_listeners", {}).values()):
        device = getattr(getattr(listener, "_endpoint", None), "device", None)
        if device is not None and hasattr(device, "platform_entities"):
            return device
    return None


def led_entity_enabled(cluster, ep_id: int) -> bool:
    """False if Home Assistant has the light entity on `ep_id` disabled.

    Home Assistant's ZHA integration mirrors each entity registry entry's
    disabled state onto the zha library entity (entity.disable()/enable()).
    True when that can't be determined, so a lookup failure never silently
    drops a LED.
    """
    device = _zha_device_of(cluster)
    if device is None:
        return True
    for entity in list(device.platform_entities.values()):
        endpoint = getattr(entity, "endpoint", None)
        if (
            str(getattr(entity, "PLATFORM", "")) == "light"
            and getattr(endpoint, "id", None) == ep_id
        ):
            return bool(entity.enabled)
    return True


class C4DimmerAllLedCluster(CustomCluster):
    """Set the LDZ-101/LSZ-101 button LEDs to an exact RGB, bypassing xy.

    A light.turn_on(rgb_color=...) on the LED light entities goes through
    Home Assistant's RGB -> xy conversion, which keeps only hue/saturation
    and drops how dark the color is — the quirk then rebuilds RGB from xy
    plus the entity's current brightness (usually 254), so every dark color
    reached the LED at full brightness (e.g. (40,0,0) -> ff0000). This
    sends the requested bytes as-is instead, the same way the KPZ-6B1's
    C4KeypadAllLedCluster always has, and syncs each LED entity's cache.

    Each LED is its own wire command, so LEDs whose light entity is
    disabled in Home Assistant are skipped (fewest commands, fastest
    response). `leds` optionally narrows the set further (bit order:
    DIMMER_ALL_LED_MASK_EPS, default all four). Lives on EP197 of the
    dimmer and the switch; call via zha.issue_zigbee_cluster_command
    (endpoint 197, cluster 0xFC48, command 0).
    """

    cluster_id = C4_ALL_LED_CLUSTER_ID
    name = "Control4 Dimmer All-LED Control"
    ep_attribute = "c4_dimmer_all_led"
    _c4_custom_handler = False  # no inbound packets for this cluster

    class ServerCommandDefs(BaseCommandDefs):
        set_all_colors = ZCLCommandDef(
            id=0x00,
            schema={
                "red": t.uint8_t,
                "green": t.uint8_t,
                "blue": t.uint8_t,
                "leds": t.uint8_t,
            },
            is_manufacturer_specific=True,
        )

    async def set_all_colors(self, red, green, blue, leds=0x0F):
        rgb = (int(red), int(green), int(blue))
        rgb_hex = "%02x%02x%02x" % rgb
        mask = int(leds)
        for bit, ep_id in enumerate(DIMMER_ALL_LED_MASK_EPS):
            if not mask & (1 << bit):
                continue
            if not led_entity_enabled(self, ep_id):
                _LOGGER.debug(
                    "C4 dimmer_all_led: skipping endpoint %d (entity disabled)",
                    ep_id,
                )
                continue
            ep = self.endpoint.device.endpoints.get(ep_id)
            color = ep.in_clusters.get(Color.cluster_id) if ep is not None else None
            if color is None:
                _LOGGER.warning(
                    "C4 dimmer_all_led: no LED color cluster on endpoint %d",
                    ep_id,
                )
                continue
            if await color._send_color(rgb_hex):
                sync_led_entity(ep, *rgb)

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        _LOGGER.debug(
            "C4 dimmer_all_led: unexpected inbound request hdr=%s args=%s",
            hdr, args,
        )
