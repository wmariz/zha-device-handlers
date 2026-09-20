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

  - Per-button LED "on" color entities use OnOff + LevelControl + a Color
    cluster advertising XY_attributes (not Hue_and_saturation — this
    zha version's light platform only ever checks the XY capability bit
    and has no HS branch at all), with LevelControl's brightness doubling
    as the color conversion's brightness/Y component so 0% naturally
    sends black, matching Control4's own "black = off" convention for
    these LEDs. See c4_led_rgb.py (reused directly for C4LedOnOff/
    C4LedLevelControl/C4LedColorCluster) for the full history, including
    the on()-resend race condition already fixed there.

Protocol CONFIRMED from a real HC300 controller log (pairing + button
presses + LED color changes in Composer):
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

  LED color (namespace family c4.kp.l*):
    c4.kp.lo <btn> <rrggbb>  — on-color   (CONFIRMED, wired to an entity)
    c4.kp.lf <btn> <rrggbb>  — off-color  (CONFIRMED, not wired to
                               anything yet — same "on-color first" scope
                               already applied to the dimmer)
    c4.kp.llm <btn> <0|1>    — "keypad managed" toggle, matching the
                               "Keypad Managed" checkbox seen in Composer
                               per-button (CONFIRMED, not wired to
                               anything yet — same idea as led_attached
                               on the dimmer, c4_attached_switch.py)
  Also observed but not needed: c4.kp.lv (a live/local LED color
  override, used for Composer's own identify-blink UI feedback — not a
  stored on/off color), c4.kp.bhp (a GET-only hold-period threshold,
  informational), c4.kp.of (an init/keepalive signal per button).

EP layout (mirrors control4_scene_controller.py's KC120277, since
c4_hooks.py's Patch 1 injects the same EP2/196/197 defaults for any C4
device regardless of model):
  1        — ZHA Non-Color Scene Controller (no light entity)
  2        — virtual, C4ConfigCluster
  196      — C4 network, C4ConfigCluster
  197      — C4 button, C4KeypadButtonCluster (routing hub only)
  200-205  — virtual per-button binary_sensor entities (press/release)
  210-215  — virtual per-button RGB light entities (on-color)

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
from zigpy.quirks import CustomDevice
from zigpy.zcl.clusters.general import BinaryInput, Identify

from zhaquirks.const import (
    CLUSTER_ID,
    COMMAND,
    DEVICE_TYPE,
    DOUBLE_PRESS,
    TRIPLE_PRESS,
    QUADRUPLE_PRESS,
    ENDPOINT_ID,
    ENDPOINTS,
    INPUT_CLUSTERS,
    LONG_PRESS,
    LONG_RELEASE,
    MODELS_INFO,
    OUTPUT_CLUSTERS,
    PROFILE_ID,
    SHORT_PRESS,
    SKIP_CONFIGURATION,
)

# Ensure patches are installed before this device class is used
import c4_hooks

import c4_helpers as C4
from c4_helpers import (
    C4_MANUF_CLUSTER,
    C4_PROFILE_BUTTON,
    C4_PROFILE_NETWORK,
    KPZ6B1_BUTTON_EP_MAP,
    KPZ6B1_BUTTON_MAP,
    KPZ6B1_LED_EP_MAP,
    C4DimmerManufCluster,
    C4ConfigCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import C4KeypadButtonCluster, _KPZ6B1_BUTTON_CLUSTERS
from c4_led_rgb import C4LedOnOff, C4LedLevelControl
from c4_keypad_led_rgb import _KEYPAD_LED_COLOR_CLUSTERS
from c4_hooks import _C4_MODEL_QUIRK_MAP

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Device quirk
# ---------------------------------------------------------------------------

class Control4KPZ6B1Keypad(CustomDevice):
    """Control4 C4-KPZ-6B1 Keypad (6 buttons)."""

    @classmethod
    def match(cls, device):
        model = getattr(device, "model", None)
        manuf = getattr(device, "manufacturer", None)
        _LOGGER.debug(
            "C4 KPZ-6B1.match called: model=%r manuf=%r ieee=%s",
            model, manuf, getattr(device, "ieee", "?"),
        )
        if model == "KPZ-6B1":
            _LOGGER.debug("C4 KPZ-6B1.match: accepting on model match")
            return True
        return super().match(device)

    signature = {
        "manufacturer_code": 0x1040,
        MODELS_INFO: [
            ("Control4", "KPZ-6B1"),
            (None, "KPZ-6B1"),
        ],
        ENDPOINTS: {
            1: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0101,
                INPUT_CLUSTERS:  [Identify.cluster_id, C4_MANUF_CLUSTER],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            196: {
                PROFILE_ID:      C4_PROFILE_NETWORK,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4.C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID:      C4_PROFILE_BUTTON,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4.C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    replacement = {
        SKIP_CONFIGURATION: True,
        ENDPOINTS: {
            1: {
                PROFILE_ID:  zha.PROFILE_ID,
                DEVICE_TYPE: 0x0830,   # Non-Color Scene Controller — no light entity
                INPUT_CLUSTERS: [
                    C4BasicCluster,
                    Identify.cluster_id,
                    C4DimmerManufCluster,
                ],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            2: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4ConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            196: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4ConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4KeypadButtonCluster],
                OUTPUT_CLUSTERS: [],
            },
            # Virtual per-button endpoints — one binary_sensor entity
            # each in ZHA (press/release). See C4KeypadButtonCluster /
            # KPZ6B1_BUTTON_EP_MAP.
            **{
                ep_id: {
                    PROFILE_ID:      zha.PROFILE_ID,
                    DEVICE_TYPE:     0x0000,
                    INPUT_CLUSTERS:  [_KPZ6B1_BUTTON_CLUSTERS[btn_id]],
                    OUTPUT_CLUSTERS: [],
                }
                for btn_id, ep_id in KPZ6B1_BUTTON_EP_MAP.items()
            },
            # Virtual per-button LED-color endpoints — one RGB light
            # entity each in ZHA (on-color). See c4_keypad_led_rgb.py.
            # DEVICE_TYPE matters here (unlike the button endpoints
            # above): ZHA's light-vs-switch platform tiebreak for an
            # OnOff cluster with Level/Color siblings apparently
            # consults it — COLOR_DIMMABLE_LIGHT is the CONFIRMED
            # working value from the dimmer's own LED entities.
            **{
                ep_id: {
                    PROFILE_ID:      zha.PROFILE_ID,
                    DEVICE_TYPE:     zha.DeviceType.COLOR_DIMMABLE_LIGHT,
                    INPUT_CLUSTERS:  [
                        C4LedOnOff, C4LedLevelControl,
                        _KEYPAD_LED_COLOR_CLUSTERS[btn_id],
                    ],
                    OUTPUT_CLUSTERS: [],
                }
                for btn_id, ep_id in KPZ6B1_LED_EP_MAP.items()
            },
        },
    }

    # One trigger entry per (action, button_name). CLUSTER_ID must match
    # the virtual endpoint's real cluster identity — BinaryInput — since
    # that's the cluster ZHA tags the fired zha_event with (see the
    # dimmer's own device_automation_triggers for the same reasoning).
    device_automation_triggers = {
        (_action, _btn_name): {
            COMMAND:     _action,
            CLUSTER_ID:  BinaryInput.cluster_id,
            ENDPOINT_ID: KPZ6B1_BUTTON_EP_MAP[_btn_id],
        }
        for _btn_id, _btn_name in KPZ6B1_BUTTON_MAP.items()
        for _action in ("press", SHORT_PRESS, DOUBLE_PRESS, TRIPLE_PRESS, QUADRUPLE_PRESS, LONG_PRESS, LONG_RELEASE)
    }


# ---------------------------------------------------------------------------
# Self-register with the get_device patch
# ---------------------------------------------------------------------------
_C4_MODEL_QUIRK_MAP["KPZ-6B1"] = Control4KPZ6B1Keypad
_LOGGER.info("C4 KPZ-6B1: registered KPZ-6B1 in _C4_MODEL_QUIRK_MAP")
