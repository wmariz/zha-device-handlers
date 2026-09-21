"""ZHA quirk for the Control4 C4-KPZ-6B1 Keypad (6 buttons).

Built from scratch this session, applying every lesson already CONFIRMED
on the APD120/LDZ-101 dimmer and the C4-Z2IO-ZP (see control4_dimmer.py's
own module docstring for the full history of each fix below):

  - Per-button press/release entities use a real BinaryInput cluster with
    its default ep_attribute intact, NOT EventableCluster (which fires
    only the classic zha_event bus message, never creating an entity —
    confirmed wrong on the dimmer) and NOT a custom ep_attribute override
    (which silently breaks ZHA's ClusterHandler discovery — also
    confirmed wrong on the dimmer). Per-button uniqueness comes from each
    one living on its own virtual endpoint, matching
    control4_z2io_zp.py's own working C4ContactCluster pattern.

  - Per-button LED color entities use OnOff + LevelControl + a Color
    cluster advertising XY_attributes (not Hue_and_saturation — this
    zha version's light platform only ever checks the XY capability bit
    and has no HS branch at all), with LevelControl's brightness doubling
    as the color conversion's brightness/Y component so 0% naturally
    sends black, matching Control4's own "black = off" convention for
    these LEDs. See c4_led_rgb.py (reused directly for C4LedOnOff/
    C4LedLevelControl/C4LedColorCluster) for the full history, including
    the on()-resend race condition already fixed there.

Protocol CONFIRMED from TWO real HC300 controller logs (pairing + button
presses + LED color changes in Composer, then a second capture of a
genuine "SET_ALL_LED_COLOR"/"SET_LED_COLOR" Composer script):
  Model: reported as "c4:control4_keypad:KPZ-6B1" -> sniffed model
  string "KPZ-6B1" (see c4_helpers.py's _c4_sniff_model).

  Button events (namespace family c4.kp.*, distinct from the dimmer's
  c4.dmx.*/c4.dm.* and the SR260's c4.zr.*):
    c4.kp.bb <btn>        — press-begin, fires immediately on press-down
    c4.kp.bc <btn>        — click complete (quick release, no count yet)
    c4.kp.cc <btn> <cnt>  — click-count confirmation (same shape as
                            c4.dmx.cc/c4.dm.cc elsewhere in this family)
    c4.kp.bh <btn>        — hold (fires while held)
    c4.kp.be <btn>        — hold-end (release after a hold)
  Button id is a single hex digit, 0-5 (not embedded in the namespace
  like the dimmer's c4.dm.b<n><code> — always a separate data field).

  LED color (namespace family c4.kp.l*) — the FIRST log showed
  `c4.kp.lo`/`c4.kp.lf` (on-color/off-color) ACKed on the wire, but the
  SECOND log (a real Composer script) revealed the command Control4's
  own scripts actually use is `c4.kp.lv` ("current" color — sets the
  LED immediately, independent of on/off state, confirmed via the
  ButtonStatus XML's <LEDCurColor> field). Since `lv` is strictly more
  useful and the on/off-scoped commands are not needed for this device,
  only `lv` is wired here, in two forms (see c4_keypad_led_rgb.py):
    c4.kp.lv <btn_2digit_hex> <rrggbb>                — single button
    c4.kp.lv ff ff <c1> <c2> <c3> <c4> <c5> <c6>      — all 6 at once
                                                          (literal "ff ff"
                                                          sentinel, then
                                                          buttons 1-6 in
                                                          order)
    c4.kp.llm <btn> <00|01>  — "keypad managed" toggle, matching the
                               "Keypad Managed" checkbox seen per-button
                               in Composer (CONFIRMED from six separate
                               captured commands, one per button — note
                               the button index here is a single
                               UNPADDED hex digit "0".."5", unlike lv's
                               zero-padded "00".."05"). CONFIRMED WRONG
                               once: this used to force every button to
                               managed=01 automatically, on the
                               assumption "managed" meant "an external
                               controller drives this LED" — the wrong
                               reading. A real Composer LED-properties
                               panel capture showed the checkbox instead
                               changes what the two color properties
                               MEAN: unmanaged, they're "On Color"/
                               "Off Color" (a static color tied to a
                               bound device's state); managed, they
                               become "Push Color"/"Release Color" (a
                               momentary flash while held, reverting on
                               release) — exactly the flash-then-revert
                               bug seen on real hardware. Fixed by
                               forcing managed=00 instead (see
                               c4_button_cluster.py's
                               C4KeypadButtonCluster.handle_message/
                               _ensure_all_buttons_unmanaged), once per
                               device automatically on first contact.
  Also observed but not needed: c4.kp.bhp (a GET-only hold-period
  threshold, informational), c4.kp.of (an init/keepalive signal per
  button).

  NOT YET CONFIRMED: "Follow Bound Color", the other per-button checkbox
  seen alongside "Keypad Managed" in Composer. It stayed True throughout
  both captured logs — never toggled — so there is no wire command for
  it yet, and no entity is exposed for it.

EP layout (mirrors control4_scene_controller.py's KC120277, since
c4_hooks.py's Patch 1 injects the same EP2/196/197 defaults for any C4
device regardless of model):
  1        — ZHA Non-Color Scene Controller (no light entity)
  2        — virtual, C4ConfigCluster
  196      — C4 network, C4ConfigCluster
  197      — C4 button, C4KeypadButtonCluster + C4KeypadAllLedCluster
             (routing hub + "set all 6 LED colors" service call)
  200-205  — virtual per-button binary_sensor entities (press/release)
  210-215  — virtual per-button RGB light entities (current color)

Uses SKIP_CONFIGURATION to prevent ZHA from attempting bind/configure on
this C4 proprietary device. The coordinator handshake is handled
reactively by _c4_sniff_model() when the device broadcasts its model
string, same as every other device in this family.
"""

import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.profiles import zha
from zigpy.quirks.v2 import QuirkBuilder
from zigpy.zcl.clusters.general import BinaryInput

from zhaquirks.const import (
    CLUSTER_ID,
    COMMAND,
    DOUBLE_PRESS,
    TRIPLE_PRESS,
    QUADRUPLE_PRESS,
    ENDPOINT_ID,
    LONG_PRESS,
    LONG_RELEASE,
    SHORT_PRESS,
)

# Ensure patches are installed before this quirk is registered
import c4_hooks

import c4_helpers as C4
from c4_helpers import (
    KPZ6B1_BUTTON_EP_MAP,
    KPZ6B1_BUTTON_MAP,
    KPZ6B1_LED_EP_MAP,
    C4DimmerManufCluster,
    C4ConfigCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import C4KeypadButtonCluster, _KPZ6B1_BUTTON_CLUSTERS
from c4_led_rgb import C4LedOnOff, C4LedLevelControl
from c4_keypad_led_rgb import _KEYPAD_LED_COLOR_CLUSTERS, C4KeypadAllLedCluster
from c4_hooks import _C4_MODEL_QUIRK_MAP

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Device quirk (QuirkBuilder v2)
#
# EP1, EP196, EP197 are all real, normally-interviewed endpoints (EP196/197
# populated by c4_hooks.py's Endpoint.initialize patch, since they never
# answer Simple_Desc_req). Their profile/device_type is forced to the
# standard ZHA profile (EP1's device_type also changes, from 0x0101 to the
# Non-Color Scene Controller type 0x0830 — no light entity) and their one
# real wire cluster is swapped for the ZHA-side virtual cluster, exactly
# matching the original CustomDevice replacement dict. There is no EP2 for
# this model (unlike the dimmer/switch/outlet quirks). Every virtual
# per-button/LED endpoint below never existed in the old signature at all —
# same "declared only in replacement" pattern used elsewhere in this fork.
# ---------------------------------------------------------------------------

_c4_kpz6b1_entry = (
    QuirkBuilder(manufacturer="Control4", model="KPZ-6B1")
    .skip_configuration()
    # --- EP1: real endpoint, device_type AND clusters change ---
    .replaces_endpoint(1, profile_id=zha.PROFILE_ID, device_type=0x0830)
    .adds(C4BasicCluster, endpoint_id=1)
    .replaces(C4DimmerManufCluster, endpoint_id=1)
    # --- EP196: real endpoint, injected at interview time ---
    .replaces_endpoint(196, profile_id=zha.PROFILE_ID, device_type=0x0000)
    .removes(C4.C4_CLUSTER_ID, endpoint_id=196)
    .adds(C4ConfigCluster, endpoint_id=196)
    # --- EP197: real endpoint, injected at interview time ---
    .replaces_endpoint(197, profile_id=zha.PROFILE_ID, device_type=0x0000)
    .removes(C4.C4_CLUSTER_ID, endpoint_id=197)
    .adds(C4KeypadButtonCluster, endpoint_id=197)
    .adds(C4KeypadAllLedCluster, endpoint_id=197)
)

# Virtual per-button endpoints — one binary_sensor entity each in ZHA
# (press/release). See C4KeypadButtonCluster / KPZ6B1_BUTTON_EP_MAP.
for _btn_id, _ep_id in KPZ6B1_BUTTON_EP_MAP.items():
    _c4_kpz6b1_entry = (
        _c4_kpz6b1_entry
        .adds_endpoint(_ep_id, profile_id=zha.PROFILE_ID, device_type=0x0000)
        .adds(_KPZ6B1_BUTTON_CLUSTERS[_btn_id], endpoint_id=_ep_id)
    )

# Virtual per-button LED-color endpoints — one RGB light entity each in ZHA
# (current color). See c4_keypad_led_rgb.py. DEVICE_TYPE matters here
# (unlike the button endpoints above): ZHA's light-vs-switch platform
# tiebreak for an OnOff cluster with Level/Color siblings apparently
# consults it — COLOR_DIMMABLE_LIGHT is the CONFIRMED working value from
# the dimmer's own LED entities.
for _btn_id, _ep_id in KPZ6B1_LED_EP_MAP.items():
    _c4_kpz6b1_entry = (
        _c4_kpz6b1_entry
        .adds_endpoint(
            _ep_id,
            profile_id=zha.PROFILE_ID,
            device_type=zha.DeviceType.COLOR_DIMMABLE_LIGHT,
        )
        .adds(C4LedOnOff, endpoint_id=_ep_id)
        .adds(C4LedLevelControl, endpoint_id=_ep_id)
        .adds(_KEYPAD_LED_COLOR_CLUSTERS[_btn_id], endpoint_id=_ep_id)
    )

# One trigger entry per (action, button_name). CLUSTER_ID must match the
# virtual endpoint's real cluster identity — BinaryInput — since that's the
# cluster ZHA tags the fired zha_event with (see the dimmer's own
# device_automation_triggers for the same reasoning).
_c4_kpz6b1_entry = (
    _c4_kpz6b1_entry
    .device_automation_triggers(
        {
            (_action, _btn_name): {
                COMMAND:     _action,
                CLUSTER_ID:  BinaryInput.cluster_id,
                ENDPOINT_ID: KPZ6B1_BUTTON_EP_MAP[_btn_id],
            }
            for _btn_id, _btn_name in KPZ6B1_BUTTON_MAP.items()
            for _action in ("press", SHORT_PRESS, DOUBLE_PRESS, TRIPLE_PRESS, QUADRUPLE_PRESS, LONG_PRESS, LONG_RELEASE)
        }
    )
    .add_to_registry()
)


# ---------------------------------------------------------------------------
# Self-register with the get_device patch
# ---------------------------------------------------------------------------
_C4_MODEL_QUIRK_MAP["KPZ-6B1"] = _c4_kpz6b1_entry
_LOGGER.info("C4 KPZ-6B1: registered KPZ-6B1 in _C4_MODEL_QUIRK_MAP")
