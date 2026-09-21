# Control4 ZHA Quirks

> **This is the `arthouse` branch** — a trimmed-down copy of the
> [`control4`](https://github.com/wmariz/zha-device-handlers/tree/control4/zhaquirks/control4)
> branch containing quirks only for the specific Control4 devices installed
> in this house: **LDZ-101, LSZ-101, LOZ-5D1-W, LOZ-5S1-W, and KPZ-6B1**.
> For the full device lineup (fan controllers, scene controllers, the
> Z2IO-ZP IO module, the SR260 remote, etc.), see the `control4` branch.

ZHA device handler quirks for Control4 Zigbee devices. These quirks allow
Control4 dimmers, switches, dimming/switched outlets, and keypads to work
with Home Assistant's ZHA integration — no Control4 controller required.

> **Disclaimer:** These quirks were developed through independent reverse
> engineering of Control4's proprietary Zigbee protocol. They are not official
> Control4 software and are not endorsed by or affiliated with Control4
> Corporation. Protocol details are based on observed traffic and may be
> incomplete or inaccurate. This software is provided "as is" without warranty
> of any kind. Use at your own risk.

## Supported Devices

| Model | Type | HA Entities |
|-------|------|-------------|
| LDZ-101 (C4-APD120) | In-Wall Adaptive Phase Dimmer | Light (dimmable), 2 hardware-config switches (button/LED attached), 4 per-button RGB LED lights (top/bottom, on/off-color) |
| LSZ-101 (C4-SW120277) | In-Wall On/Off Switch | Switch |
| LOZ-5D1-W | Dual Dimming Outlet | 2 lights (dimmable, one per outlet) |
| LOZ-5S1-W | Dual Switched Outlet | 2 switches (one per outlet) |
| KPZ-6B1 | 6-Button Zigbee Keypad | 6 binary sensors (press/release), 6 per-button RGB LED lights (current color) |

> **loz-5d1-w note:** outlet 1 (EP1) sends real ZCL Level Control frames,
> the same way the confirmed LDZ-101 dimmer does. Outlet 2 (synthetic
> EP11) has no real Zigbee endpoint of its own, so it can't receive a real
> ZCL frame; it speaks the outlet's own `c4.dm.tv <outlet> 00 <level>` text
> command instead (same shape as the already-confirmed on/off command,
> just with a graduated value). **Both outlets are confirmed working on
> real hardware** for graduated dimming, including dragging the brightness
> slider while a light is on.
>
> Getting graduated dimming working took ten attempts, most of them
> chasing the wrong layer — see the module docstring's "History" section
> for the full trail if you're touching this file. The short version: the
> wire command was correct from very early on (confirmed by connecting the
> physical device to a real HC-300 controller and decoding its driver log
> byte-for-byte), but this quirk's own command handler had a real bug — it
> read the requested brightness from a positional argument that arrives
> empty on newer zigpy/Python stacks (the level comes through as a
> `level=` keyword instead), so every dim request silently sent "off"
> regardless of the value requested.
>
> **A brief "flash" of a different brightness right after turning a
> light back on is expected and not a bug.** Home Assistant's own ZHA
> integration optimistically displays a cached brightness on turn-on
> (`off_brightness` if the light was turned off with a transition/fade,
> its own internal brightness cache otherwise) before the real level
> comes back — confirmed by checking Developer Tools -> States directly.
> This lives in HA core/ZHA, not in this quirk, and cannot be changed
> here. What matters is what it settles on afterward, which took three
> rounds of fixes to get right on both outlets:
>
> 1. Outlet 1 could settle on the wrong *final* brightness (e.g. ~73%
>    instead of the correct level) after being dimmed, turned off, then
>    back on. The real dimming circuit sends several graduated
>    `c4.dm.tc` announcements while ramping (e.g. 97%, 34%, 0%, 3%, 98%
>    in quick succession), and an earlier revision synced outlet 1's
>    brightness from every one of them, racing against the more reliable
>    real-ZCL update. Fixed by driving outlet 1's brightness solely from
>    the real ZCL passthrough again; outlet 2 (no real Zigbee endpoint,
>    no other source of truth) keeps syncing from `c4.dm.tc`.
>
> 2. Outlet 1's plain on/off toggle (not the brightness slider) settled
>    at a fixed ~75% instead of restoring the brightness from before it
>    was turned off. Checking Home Assistant's own ZHA source confirmed
>    that restoring the previous brightness — not jumping to a fixed
>    value — is the standard behavior for a plain turn-on. Fixed by
>    having outlet 1 remember its last non-zero level and restore it.
>
> 3. Outlet 2 had the identical bug in a different shape: its plain
>    on/off toggle always forced the outlet fully on (100%) instead of
>    restoring its pre-off level, because its wire transport is shared
>    with the non-dimmable LOZ-5S1-W switch (where 100%/0% is the only
>    correct behavior). Fixed the same way as outlet 1: outlet 2 now
>    remembers and restores its own last non-zero level too.
>
> Both outlets now consistently restore their previous brightness on a
> plain on/off toggle. See the module docstring's "History" (attempts
> 13–17) for the full trail, including two wrong turns along the way
> (forcing outlet 1 to always 100%, then a same-day revert once outlet 2
> turned out to still need the same fix) before landing here. Please
> open an issue (ideally with an HA debug log for
> `control4_outlet_dimmer`) if either outlet still misbehaves.

All Control4 Zigbee devices use a proprietary text-based serial protocol
layered on top of ZigBee APS instead of standard ZCL clusters. These quirks
translate between that protocol and the ZCL interfaces that ZHA expects.

## Installation

These quirks live inside the `zha-device-handlers` package. To install:

1. Copy the entire `zhaquirks/control4/` directory into your Home Assistant
   custom quirks folder. The default location is:

   ```
   <config>/custom_zha_quirks/control4/
   ```

2. Make sure custom quirks are enabled in your ZHA configuration. In
   `configuration.yaml`:

   ```yaml
   zha:
     custom_quirks_path: /config/custom_zha_quirks
   ```

3. Restart Home Assistant.

ZHA will auto-discover the quirks on startup — no additional configuration is
needed.

## Pairing a Control4 Device

Control4 devices do not use standard Zigbee pairing. They require specific
"magic button press" sequences to reset and identify, and the coordinator must
be physically close to the device during the process.

For a complete reference of button sequences across all Control4 Zigbee
products, see the
[Genesis Technologies definitive guide](https://technet.genesis-technologies.ch/control4-zigbee-the-definitive-guide/).

### Before You Start

- **Move your Zigbee coordinator close to the device.** Control4 devices
  broadcast their identity at low power during pairing. If the coordinator is
  across the house, the device may try to route through another Control4 device
  instead of talking directly to the coordinator, and it will fail to join.

- **If multiple Control4 devices share the same electrical box, pair them all
  at the same time.** A Control4 device that has already joined the network
  will act as a Zigbee router. If you pair one device and then try to pair its
  neighbor, the second device will route through the first rather than
  communicating directly with the coordinator. This causes the join to fail
  because the already-joined device speaks Control4's proprietary protocol to
  the new device instead of relaying standard Zigbee join frames. Pair all
  devices in the same box in a single session to avoid this.

### Button Sequences by Device Type

Control4 devices use two key sequences during pairing: a **factory reset**
(leave Zigbee mesh and clear all settings) and an **identify** (broadcast
identity to the coordinator). The exact button presses depend on the device
type.

**In-wall lighting (LDZ-101 dimmer, LSZ-101 switch):**

| Action | Sequence |
|--------|----------|
| Identify | 4 x top button |
| Reboot | 15 x top button |
| Reset defaults | 9 x top, 4 x bottom, 9 x top |
| Leave mesh + factory reset | 13 x top, 4 x bottom, 13 x top |

**6-button keypads (KPZ-6B1):**

| Action | Sequence |
|--------|----------|
| Identify | 4 x top-left button |
| Reboot | 15 x top-left button |
| Reset defaults | 9 x top-left, 4 x bottom-left, 9 x top-left |
| Leave mesh + factory reset | 13 x top-left, 4 x bottom-left, 13 x top-left |

**Outlets (LOZ-5D1-W, LOZ-5S1-W):**

| Action | Sequence |
|--------|----------|
| Identify | 4 x button |
| Reset defaults | 9 x button (or 15 x for some models) |
| Leave mesh + factory reset | 13 x button |

### Pairing Steps

1. **Factory-reset the device (13-4-13).** For the LDZ-101/LSZ-101: tap
   the top button 13 times, then the bottom button 4 times, then the top
   button 13 times. For the KPZ-6B1 keypad use top-left and bottom-left.
   For the outlets tap the button 13 times. The LEDs will flash to
   confirm the reset. This clears any previous network association and puts
   the device into join mode.

2. **Start the join process in Home Assistant.** Go to
   **Settings → Devices & Services → ZHA → Add Device** (or click the
   "Add Zigbee Device" button). ZHA will open the network for new devices.

3. **Trigger a 4-click identify on the device.** Tap the top button (or
   top-left button on the KPZ-6B1, or the single button on the outlets)
   4 times quickly. The device will broadcast its identity to the
   coordinator. ZHA should discover the device within a few seconds.

4. **Wait for ZHA to finish configuring the device.** The quirk will
   automatically handle the Control4 provisioning handshake (key exchange,
   button/LED configuration, transition times, etc.). This typically takes
   5–15 seconds. The device will appear in ZHA once provisioning completes.

5. **Repeat for any remaining devices in the same box** before moving the
   coordinator away.

### Other Useful Sequences

- **Reboot (15 x top):** Restarts the device without clearing settings.
  Useful if a device becomes unresponsive.

- **Reset defaults (9-4-9):** Resets configuration to factory defaults but
  does not leave the Zigbee mesh. Tap the top button 9 times, bottom 4 times,
  top 9 times.

- **Channel blink (7-4-7):** Makes the device blink its current Zigbee
  channel number on its LEDs. Tap top 7 times, bottom 4 times, top 7 times.
  Useful for verifying a device is on the expected channel.

### Troubleshooting Pairing

- **Device not discovered:** Make sure the coordinator is within a meter or two
  of the device. Try the 13-4-13 factory reset again, wait a few seconds, then
  repeat the 4-click identify.

- **Device joins but shows as "unknown" or has no entities:** The quirk may not
  have matched. Check the ZHA logs for the device's model string (it should be
  something like `c4:control4_light:C4-APD120`). Restart Home Assistant to
  force re-discovery.

- **Second device in the same box won't pair:** This is the routing problem
  described above. Remove the first device from ZHA, then pair both devices at
  the same time. Alternatively, temporarily power off the already-joined device
  by pulling the air-gap switch (if it has one) while pairing the second.

## Device-Specific Notes

### LDZ-101 Dimmer

Exposes a dimmable light entity with on/off and brightness control. The dimmer
uses adaptive phase dimming and reports real-time power telemetry. Physical
button presses on the device are handled locally and the state is synced back
to Home Assistant automatically.

Default transition times: 800 ms ramp-on, 2000 ms ramp-off, default on-level
of ~75%.

**Button events:** the top and bottom paddle buttons each get their own HA
Event entity (visible in Developer Tools -> States, with history) — not just
an automation trigger. Available actions: `press` (immediate, on physical
press-down, before a click/hold resolves), `remote_button_short_press` /
`_double_press` / `_triple_press` / `_quadruple_press` (resolved once
released), and `remote_button_long_press` / `_long_release` (holding the
button down).

> **Note:** commanding the light to 100% (the ZCL maximum, 254) settles at
> ~99% — the device's own firmware appears to compute its displayed
> percentage as level/255 rather than the ZCL-correct level/254, which no
> valid ZCL command can work around. Turning on also has a physical
> rise-time floor of roughly 0.7-1.3s regardless of the requested transition
> time, consistent with a phase dimmer's soft-start circuitry; turning off
> reasonably tracks whatever transition time is actually requested. Neither
> is a bug in this quirk.

**Hardware-config switches** — `button_attached`/`led_attached`, two
Switch entities (virtual `OnOff`-cluster endpoints 200/201,
`c4_attached_switch.py`) matching the LDZ-101's own physical DIP-style
config settings. Confirmed working via a real device ACK (`0r<seq> 000`)
in an HA debug log.

**Per-button RGB LED lights** — 4 Light entities (`c4_led_rgb.py`,
virtual endpoints 202/203 "top"/"bottom" on-color, 204/205 "top"/"bottom"
off-color), driven while `led_attached` is off (otherwise the device's
own built-in logic drives the LEDs). Getting these entities to appear at
all — and then to pick the right color model — took three wrong turns
on real hardware before landing on the working design: a bare
`OnOff`+`Color` pair gets claimed by the generic Switch platform instead
of Light; `Color` needs a sibling `LevelControl` cluster before ZHA's
light-platform discovery will treat the endpoint as a light at all
(brightness then doubles as the color conversion's Y/brightness
component, so 0% naturally sends black — Control4's own "off"
convention for these LEDs); and this `zha` version's light platform
only ever checks the `XY_attributes` color-capability bit, with no
Hue/Saturation branch anywhere in it, so `Color` must advertise XY and
implement `move_to_color` (CIE 1931 xy), not
`move_to_hue_and_saturation`. See `c4_led_rgb.py`'s own module docstring
for the full attempt-by-attempt history, including a race condition
where picking a color sent two wire commands ~150ms apart (the correct
one, then a stale one) — fixed with a timestamp-suppression window. A
"Set All LEDs" HA script (all four entities, one RGB) is included — see
[LED Configuration](#led-configuration) below.

### LSZ-101 Switch

Exposes a simple on/off switch entity. Physical button presses sync state back
to Home Assistant. Registered separately from the LDZ-101 dimmer quirk so it
correctly shows up as a Switch, not a Light.

### LOZ-5D1-W Dual Dimming Outlet

Exposes two independent dimmable light entities, one per outlet. See the
note under [Supported Devices](#supported-devices) above for the graduated
dimming details and history.

### LOZ-5S1-W Dual Switched Outlet

Exposes two independent switch entities, one per outlet. Each outlet can be
toggled individually.

### KPZ-6B1 6-Button Keypad

Exposes 6 binary_sensor entities (press/release, one per button) and 6
per-button RGB light entities (current color). Full protocol details,
including a real bug found by pulling the compiled Control4 driver off a
physical controller's recovery partition, are in
[`documentation/control4-kpz6b1-keypad-protocol.md`](documentation/control4-kpz6b1-keypad-protocol.md).

**Button events:** each button fires `press` immediately on press-down,
then one of `remote_button_short_press` / `_double_press` /
`_triple_press` / `_quadruple_press` (a resolved click) or
`remote_button_long_press` / `_long_release` (holding the button down) —
the same action set as the LDZ-101 dimmer's own button events.

**LED colors:** each button's light entity sets its own current color via
`light.turn_on`. For setting multiple buttons at once, use
`C4KeypadAllLedCluster`'s two cluster commands (endpoint 197, cluster
`0xFC48`) instead of six separate `light.turn_on` calls — see the two
ready-made scripts below.

**"Keypad Managed" is force-disabled automatically** the first time the
device is seen after pairing (and again after any HA/ZHA restart, since
the guard is per-session). This is a fix, not a configurable option: an
earlier version of this quirk force-*enabled* it under the mistaken
belief that "managed" meant "let ZHA manage the LED" — enabling it
actually puts the device into a "Push Color"/"Release Color" mode that
flashes and reverts the LED on every physical touch, discarding whatever
color was set via a script or the light entity. See the protocol doc
linked above for the full story if this behavior ever needs revisiting.

## LED Configuration

The LDZ-101 dimmer and the KPZ-6B1 keypad have per-button RGB LED
indicators, each speaking its own protocol — a script written for one
device does not work on the other.

### `control4_ldz101_led_rgb_scripts.yaml` — LDZ-101 dimmer *or* KPZ-6B1 keypad

**`control4_ldz101_set_all_leds`** — a single script that targets either
the LDZ-101 dimmer's 4 LED light entities (top/bottom, on/off-color) or
the KPZ-6B1 keypad's 6 buttons, picking the right method automatically
based on which device you select (the device picker only allows these
two models). One RGB color field, applied to every LED entity/button on
the chosen device. For the dimmer this replicates Control4's own
SET_ALL_LED command exactly; for the keypad it calls
`C4KeypadAllLedCluster.set_all_colors`.

### `control4_kpz6b1_individual_leds_script.yaml` — KPZ-6B1 keypad only

**`control4_kpz6b1_set_individual_leds`** — device picker restricted to
KPZ-6B1 keypads, with one color field per button (6 total), calling
`C4KeypadAllLedCluster.set_individual_colors` to set all 6 to their own
distinct color in a single wire frame.

Copy either file into your Home Assistant scripts configuration, or paste
its contents into **Settings → Automations & Scenes → Scripts → Add
Script → Edit in YAML**.

## Ramp Rate Configuration (LDZ-101 dimmer)

`ha-scripts/control4_ramp_scripts.yaml` — two scripts targeting
`C4RampCluster` (endpoint 4, cluster `0xFC44`) for adjusting the LDZ-101's
transition times:

- **control4_set_ramp_rate** — set a single ramp time (on-ramp, off-ramp,
  fast, or one of two slow-fade slots) from a dropdown.
- **control4_set_on_off_ramps** — set both the on-ramp and off-ramp times
  at once.

Defaults confirmed from the device's own provisioning capture: 750 ms
on-ramp, 2000 ms off-ramp, 100 ms fast ramp, 5000 ms slow fades.

## Architecture

The quirks are organized as follows:

```
control4/
├── control4_dimmer.py           LDZ-101 (C4-APD120) quirk
├── control4_switch.py           LSZ-101 (C4-SW120277) quirk
├── control4_outlet.py           LOZ-5S1-W quirk
├── control4_outlet_dimmer.py    LOZ-5D1-W quirk (dual dimming outlet)
├── control4_keypad.py           KPZ-6B1 quirk
├── c4_basic_cluster.py          Model/manufacturer resolution for C4 devices
├── c4_button_cluster.py         Button event parsing & state sync (all devices)
├── c4_led_rgb.py                Per-button RGB light entities (LDZ-101 dimmer; shared base classes reused by the keypad)
├── c4_keypad_led_rgb.py         KPZ-6B1 per-button RGB lights + C4KeypadAllLedCluster (0xFC48)
├── c4_attached_switch.py        button_attached/led_attached hardware-config switches (dimmer)
├── c4_ramp_cluster.py           Transition-time / hardware-config cluster (dimmer provisioning)
├── c4_helpers.py                Constants, frame builders, shared utilities
├── c4_hooks.py                  Monkey-patches for quirk discovery & routing
├── ha-scripts/
│   ├── control4_ldz101_led_rgb_scripts.yaml        "Set All LEDs" — LDZ-101 dimmer or KPZ-6B1 keypad
│   ├── control4_kpz6b1_individual_leds_script.yaml "Set Individual LEDs" — KPZ-6B1 only
│   └── control4_ramp_scripts.yaml                  Ramp/transition-time scripts — LDZ-101 dimmer only
└── documentation/               Protocol documentation (from packet captures, HA/ZHA logs, and a real driver binary)
```

### How It Works

Control4 devices communicate using a proprietary ASCII serial protocol
tunneled over ZigBee APS frames on custom profiles (`0xC25C`, `0xC25D`,
`0xC25E`). They do not use standard ZCL clusters for device control.

The quirks work by:

1. **Intercepting packets** — `c4_hooks.py` patches zigpy's packet handling to
   recognize Control4 profiles and route them to the correct quirk clusters.

2. **Translating protocols** — The button and LED clusters parse incoming C4
   ASCII frames (like `sa c4.dmx.ls 00 00 64 ...`) and update the standard
   ZCL attribute caches (OnOff, LevelControl) that ZHA reads.

3. **Sending commands** — When you toggle a light or change brightness in HA,
   the quirk builds C4-format ASCII command frames and sends them over the
   proprietary APS profile.

4. **Caching identity** — `c4_basic_cluster.py` maintains a persistent
   IEEE-to-model map so devices are correctly identified across restarts,
   stored in `/config/.storage/c4_quirk_data.json`.
