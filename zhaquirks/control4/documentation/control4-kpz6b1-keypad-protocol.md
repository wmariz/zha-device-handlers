# Control4 KPZ-6B1 6-Button Keypad — Protocol & Implementation Notes

> **Disclaimer:** This document was produced through independent analysis
> of real HC300 controller logs, HA/ZHA debug logs, and a compiled Control4
> driver binary pulled from a physical HC1000v2 controller's recovery
> partition. It is not official Control4 documentation and is not endorsed
> by or affiliated with Control4 Corporation. All protocol details, command
> names, and behavioral descriptions are based on observed traffic/binaries
> and may be incomplete or inaccurate. This information is provided "as is"
> without warranty of any kind. Use at your own risk.

## Device Identity

Reported over the wire as `c4:control4_keypad:KPZ-6B1`, sniffed down to the
model string `KPZ-6B1` by `c4_helpers.py`'s `_c4_sniff_model()` (the same
generic sniffer every device in this family uses). Manufacturer code
`0x1040`.

## Transport

Same ASCII-over-APS transport as every other device in this family —
`0x[sd]/[t]/[r][seq4] [namespace] [args...]\r\n` on `C4_PROFILE_BUTTON`,
`C4_CLUSTER_ID`, endpoint 197.

## Button Events — `c4.kp.*`

Distinct namespace family from the dimmer's `c4.dmx.*`/`c4.dm.*` and the
SR260's `c4.zr.*`. Button id is a single hex digit (`0`–`5`), always a
*separate* data field — not baked into the namespace the way the dimmer's
`c4.dm.b<n><code>` is.

| Command | Direction | Description |
|---|---|---|
| `c4.kp.bb <btn>` | Device → Coordinator | Press-begin — fires immediately on press-down |
| `c4.kp.bc <btn>` | Device → Coordinator | Click complete — sent the moment a quick press is released, before the count is known (`cc` always follows) |
| `c4.kp.cc <btn> <count>` | Device → Coordinator | Click-count confirmation, sent only after the multi-click window closes (same shape as `c4.dmx.cc`/`c4.dm.cc` elsewhere in this family). Ignored by the quirk. |
| `c4.kp.bh <btn>` | Device → Coordinator | Hold (repeats while held) |
| `c4.kp.be <btn>` | Device → Coordinator | Hold-end (release after a hold) |
| `c4.kp.bhp` | Coordinator → Device (Get) | Hold-period threshold, e.g. `01f4` = 500ms |
| `c4.kp.of <btn>` | Device → Coordinator | `0i`-prefixed init/keepalive signal per button |

A real compiled driver (`control4_keypad.c4l`, see below) also contains
literal wire strings for a *second*, per-button-embedded button-event
family — `c4.kp.b0b`/`c4.kp.b0c`/`c4.kp.b0e` … `c4.kp.b5b`/`c4.kp.b5c`/
`c4.kp.b5e` (mirroring the dimmer's own `c4.dm.b<n><code>` shape) — but
every real capture from this specific KPZ-6B1 unit used the generic
`bb`/`bc`/`be`/`bh`/`cc` form with the button id as a separate argument.
The per-button-embedded form is presumably used by some other keypad SKU
this same generic driver also supports.

## LED Color — `c4.kp.l*`

**CONFIRMED from TWO real HC300 controller logs**, and cross-checked
against the real compiled driver (`control4_keypad.c4l`):

| Command | Meaning |
|---|---|
| `c4.kp.lv <btn_2digit_hex> <rrggbb>` | Set button `<btn>`'s **current** displayed color immediately, independent of on/off state |
| `c4.kp.lv ff ff <c1> <c2> <c3> <c4> <c5> <c6>` | Set all 6 buttons' current color in one frame (literal `ff ff` sentinel, then buttons 1–6 in order) — maps to Composer's `SET_ALL_LED_COLOR` action |
| `c4.kp.lo <btn_2digit_hex> <rrggbb>` | Set button `<btn>`'s persistent **on-color** (Composer property `LedOnColor`) |
| `c4.kp.lf <btn_2digit_hex> <rrggbb>` | Set button `<btn>`'s persistent **off-color** (Composer property `LedOffColor`) |

The first captured log showed `c4.kp.lo`/`c4.kp.lf` ACKed on the wire (from
manually editing the Composer LED-color properties). A second log —
captured from a genuine Composer script using the `SET_ALL_LED_COLOR`
action — revealed `c4.kp.lv` as the command Control4's own scripts
actually use for a one-off color change, confirmed via the ButtonStatus
XML's `<LEDCurColor>` field tracking it exactly, independent of
`<CurState>`. Every captured `lv ff ff ...` example repeated the *same*
color across all 6 slots (matching the Composer action's own wording,
"Set all LED current colors to `<color>`"), even though the wire format
itself carries 6 independent slots.

This quirk exposes only `lv` (both forms) — see
[Push/Release vs. On/Off color and the "Keypad Managed" bug](#push-release-vs-onoff-color-and-the-keypad-managed-bug)
below for why `lo`/`lf` were deliberately left unused, and how a related
misunderstanding about "Keypad Managed" caused a real bug.

## Keypad Managed — `c4.kp.llm`

`c4.kp.llm <btn_single_hex_digit> <00|01>` — note the button index here is
a **single, unpadded** hex digit (`0`.."5"), unlike `lv`/`lo`/`lf`'s
zero-padded `00`.."05". Confirmed from six separate captured commands, one
per button, toggling the "Keypad Managed" checkbox in Composer.

### Push/Release vs. On/Off color, and the "Keypad Managed" bug

**This checkbox does not mean "let an external controller manage this
LED."** A real Composer LED-properties-panel capture showed that toggling
it changes what the *other two color properties on that same panel mean*:

| Keypad Managed | Color property labels | Meaning |
|---|---|---|
| Unchecked (default) | **On Color** / **Off Color** | A static color tied to a bound device's on/off state (`c4.kp.lo`/`c4.kp.lf`) |
| Checked | **Push Color** / **Release Color** | A momentary flash while the button is physically held, reverting to the release color on release |

This quirk originally force-set every button to `llm <btn> 01` (managed
= ON) automatically on first contact, on the wrong assumption that
"managed" meant "an external system drives the LED, so make sure it's
in that mode." That is exactly backwards: managed mode makes the device's
own firmware locally override the displayed LED color with a "push
color" for the duration of every physical press, then a "release color"
on release — which silently overwrote any color previously set via
`c4.kp.lv` the instant the button was touched (the LED would flash, then
revert, with **zero accompanying Zigbee traffic** — the whole thing
happens locally in the device's firmware).

**Fixed** by forcing `llm <btn> 00` (managed = OFF / unmanaged) instead,
once per device on first contact
(`c4_button_cluster.py`'s `C4KeypadButtonCluster._ensure_all_buttons_unmanaged()`).
Confirmed working on real hardware: a color set via `set_all_colors`/
`set_individual_colors` now survives physical button presses.

This bug was found by pulling the **real compiled Control4 keypad
driver** off a physical HC1000v2 controller's recovery partition
(`I:\boot\hc1000v2\recovery\recovery~\control4\drivers\control4_keypad.c4l`)
and reading its C++ symbol table and embedded string literals directly —
no disassembler needed, just extracting printable ASCII runs from the raw
bytes (e.g. via PowerShell regex, since this environment had no
`strings`/`objdump`). Relevant symbols found: `KeypadButton::GetOnColor`/
`GetOffColor`/`GetCurColor`, `SetLocalLEDManaged`/`IsLocalLEDManaged`,
`SetFollowBoundColor`/`DoFollowBoundColor`, `control4_keypad::
SetLedStateColor(int state)` (picks on/off color based on the bound
device's state), and literal command strings including `SET_LED_ON`,
`SET_LED_OFF`, `SET_MANAGED_STATE`, `MATCH_LED_STATE`,
`FORCE_MANAGED_MODE`. When a proprietary wire-protocol mystery resists
log-only diagnosis, checking whether the real driver binary exists on a
controller's recovery partition can be far faster than more rounds of
log-capture guessing.

### "Follow Bound Color"

The other per-button checkbox seen alongside "Keypad Managed" in
Composer. **Not confirmed** — it stayed `True` throughout every captured
log, never toggled, so there is no known wire command for it. Per the
driver binary's `SetFollowBoundColor`/`DoFollowBoundColor` symbols, it
appears to control whether a button's color automatically follows the
color of *another bound light* rather than using its own stored on/off
color — not directly relevant to a plain ZHA install with no Control4
room/binding concept. No entity is exposed for it in this quirk.

## Other observed commands (not wired — informational only)

Found via the real driver binary but not confirmed on the wire from this
specific unit — likely used for GET-style state queries the controller
issues to resync its own knowledge of the device (e.g. after a reconnect),
not needed for driving colors:

| Command | Likely purpose |
|---|---|
| `c4.kp.ls`, `c4.kp.ls0`, `c4.kp.ls3` | Get LED state (per-button or batched) — driver symbols `OnRspLedState0`/`OnRspLedState3`/`OnRspAllLedState` |
| `c4.kp.s` | Get overall keypad state — driver symbol `OnRspKeypadState` |
| `c4.kp.on` | Possibly a device "online"/boot announcement, counterpart to `c4.kp.of` |

## HA Entities

- **6 binary_sensor entities** (press/release) — `BinaryInput` clusters on
  virtual endpoints 200–205, one per button. Present-value goes `True` on
  `press` (`c4.kp.bb`) and `False` on release: a click via `c4.kp.bc`
  (which also fires `remote_button_short_press`), or a hold ending via
  `c4.kp.be`. `c4.kp.cc` is ignored, so double/triple clicks aren't
  reported — each click is its own `short_press`.
- **6 RGB light entities** (current color) — `OnOff` + `LevelControl` +
  `Color` (XY mode) on virtual endpoints 210–215, one per button. See
  `c4_led_rgb.py`'s module docstring for the general RGB-light-entity
  design (shared with the APD120/LDZ-101 dimmer) and its own history of
  fixes.
- **`C4KeypadAllLedCluster`** (endpoint 197, manufacturer-specific cluster
  `0xFC48`) — exposes two ZHA cluster commands for setting all 6 LEDs in
  a single wire frame, faster than one `light.turn_on` call per button:
  - `set_all_colors(red, green, blue)` — one color, repeated across all 6
    buttons (`c4.kp.lv ff ff <c>x6`).
  - `set_individual_colors(red_1, green_1, blue_1, ..., red_6, green_6,
    blue_6)` — 6 independent colors in one frame.

  Both bypass the per-button light entity clusters entirely (writing
  straight to the wire with zero RGB conversion loss), so
  `C4KeypadAllLedCluster._sync_button_entity()` separately pushes the
  just-sent color into each affected button's Color/Level/OnOff cache
  afterwards, to keep the HA UI consistent. See the two ha-scripts below
  for the recommended way to drive these from Home Assistant.

## The HA color-display bug (ZHA-side, not this device's protocol)

Setting a color via `set_all_colors`/`set_individual_colors` correctly
reached the physical LED every time (zero conversion — the exact RGB
bytes are sent as-is) but the corresponding light entity's card in the HA
UI would not show it, sometimes for up to an hour. Root causes — general
ZHA/`zigpy`/`zha-device-handlers` knowledge, not specific to this device:

1. **`zha`'s light platform only refreshes a light's Color-cluster
   attributes on a periodic poll — every 45–75 *minutes*.** Brightness and
   on/off push to the UI instantly via event listeners; color does not.
   Any code that updates `current_x`/`current_y` outside of a genuine
   `move_to_color` ZCL command (e.g. this cluster's `set_all_colors`) will
   not show up until that slow poll happens to land.
2. That poll always issues a real, cache-bypassing ZCL read
   (`safe_read(cluster, [...], allow_cache=False, only_cache=False)`). A
   synthetic Color cluster with no real on-device ZCL backing can never
   answer that, so the poll silently times out — every cycle, forever.
3. Forcing that poll to run immediately *from Python* (via a `hass`
   handle obtained from the zigpy device object, then calling the
   `homeassistant.update_entity` service) does not work: in this
   rewritten `zha` package, the `Gateway` holds `hass` only via its own
   `config` object, with no reachable back-reference from the zigpy
   `ControllerApplication`/device side. Fixed instead by adding a
   `homeassistant.update_entity` step directly in the **calling HA
   script** (see the two scripts below) — a script naturally has `hass`
   access, no internals-walking needed.
4. Even with the poll now running, it still failed at first: in this
   zigpy version, `CustomCluster` actually lives in `zha-device-handlers`'s
   own `zhaquirks.clusters` module (`zigpy.quirks` is now just a
   deprecation shim re-exporting it), and `_CONSTANT_ATTRIBUTES` is only
   consulted inside `read_attributes_raw()` — a method the base
   `Cluster.read_attributes()` never reaches once `only_cache=True`
   short-circuits first. A `color_mode` declared only via
   `_CONSTANT_ATTRIBUTES` therefore never resolved through a forced
   cache-only read.
5. **Final fix**: `C4LedColorCluster`/`C4LedLevelControl`/`C4LedOnOff`
   (in `c4_led_rgb.py`, shared with the dimmer) now share one mixin,
   `_C4LocalOnlyReadMixin`, whose `read_attributes()` never delegates to
   the base implementation at all — it resolves every requested attribute
   via `self.get()` (the same mechanism already used throughout this
   project to read a sibling cluster's cached value).
6. Separately, the RGB↔xy math used to keep a light entity's cache in
   sync with a directly-set RGB must use the exact same matrix Home
   Assistant itself uses to render an xy color — the "Wide RGB D65"
   formula (`homeassistant.util.color.color_RGB_to_xy_brightness`), not
   the standard/narrow sRGB D65 matrix (which the wire-side conversion in
   `c4_led_rgb.py` correctly uses instead, since that's a different
   concern — a real hardware round-trip, not a UI-display round-trip).

## HA Scripts

Two ready-to-use scripts ship in `ha-scripts/`:

- **`control4_ldz101_led_rgb_scripts.yaml`** (`control4_ldz101_set_all_leds`)
  — device picker restricted to LDZ-101 dimmers *or* KPZ-6B1 keypads
  (filtered by manufacturer `Control4` + model). Detects which one was
  picked and calls the right method automatically: the dimmer's own
  4-entity `light.turn_on`, or this keypad's `set_all_colors`.
- **`control4_kpz6b1_individual_leds_script.yaml`**
  (`control4_kpz6b1_set_individual_leds`) — KPZ-6B1-only device picker,
  one color field per button, calls `set_individual_colors` for all 6 in
  one frame.

Both include the `homeassistant.update_entity` step needed to make the HA
UI reflect the new color immediately (see the bug writeup above).
