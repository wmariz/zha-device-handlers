"""ZHA quirk for the Control4 LOZ-5D1-W Dimming Outlet.

ATTEMPT 3 — real ZCL Level Control passthrough for outlet 0.

Two previous attempts (see git history: commit 438bb9a, and the commit
that replaced it) implemented brightness by translating LevelControl
commands into the outlet's c4.dm.tv text SET command with a graduated
0-100 level, exactly like C4OutletOnOff does for on/off in
control4_outlet.py. Real-hardware testing showed this does not work: any
level other than 0x00/0x64 makes the light revert to (or stay) off, and a
plain turn-on falls back to C4_DEFAULT_ON_LEVEL (191 -> 75%), which is why
the UI got stuck showing 75% brightness whenever the light was on. Both
attempts shared this same wrong premise, so both failed identically.

This attempt is based on re-reading C4DimmerOnOff's docstring in
control4_dimmer.py:

    "C4 dimmers ignore standard On/Off (cluster 0x0006) commands but
    respond to move_to_level_with_on_off (cluster 0x0008, cmd 0x04)."

That describes the confirmed, real C4-APD120 behavior: LevelControl
commands travel to the device as genuine ZCL frames (cluster 0x0008), not
as a synthesized text command. The LOZ-5S1-W's own interview already
reports a native LevelControl cluster on EP1 (see control4_outlet.py's
docstring: "clusters [Basic … OnOff Level Time]") — the switch quirk just
never wires it up because the switch doesn't dim. It's plausible the
LOZ-5D1-W's dimming circuit responds to real ZCL Level Control the same
way the APD120 does, and the c4.dm.tv text channel was never the right
transport for brightness at all — only ever confirmed for plain on/off.

This version therefore:
  • Outlet 0 (EP1) reuses C4DimmerOnOff / C4DimmerLevelControl UNCHANGED
    from control4_dimmer.py — no override, no text-command translation.
    LevelControl commands go out as real ZCL frames, exactly like the
    APD120. EP2/EP196 also reuse the base C4ConfigCluster (not
    C4OutletConfigCluster), matching the APD120's raw 0-255 dim-level
    report path (_sync_ep1_level) instead of the outlet's on/off-flag
    interpretation (0/2).
  • Outlet 1 (synthetic EP11) has no physical endpoint of its own — it is
    a software-only construct so a second HA entity can control the
    second relay — so it cannot receive a real ZCL frame. It keeps the
    CONFIRMED-working on/off-only c4.dm.tv transport from
    control4_outlet.py (C4Outlet1OnOff), unchanged, and is exposed as a
    plain on/off switch, not a light, until there's a reason to believe
    it can dim independently.

STILL UNVERIFIED — please re-test and report back:
  • Whether outlet 0 actually responds to real ZCL Level Control frames
    (turn on at an arbitrary brightness, then drag the slider while on).
  • Whether EP2/EP196 reports a graduated 0-255 level for this device the
    way the APD120 does, rather than the outlet's 0/2 flag (affects how
    quickly/accurately the UI reflects state changed locally at the
    device, e.g. by a Control4 keypad).

If this attempt *also* gets stuck at a fixed level or reverts to off, the
device most likely does not implement real ZCL Level Control either, and
the only way forward is a Wireshark capture of a real Control4 controller
dimming this specific device.
"""

import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.profiles import zha
from zigpy.quirks import CustomDevice
from zigpy.zcl.clusters.general import (
    Basic, Groups, Identify, LevelControl, OnOff, Scenes, Time,
)

from zhaquirks.const import (
    CLUSTER_ID,
    COMMAND,
    DEVICE_TYPE,
    ENDPOINT_ID,
    ENDPOINTS,
    INPUT_CLUSTERS,
    MODELS_INFO,
    OUTPUT_CLUSTERS,
    PROFILE_ID,
    SKIP_CONFIGURATION,
)

# Ensure patches are installed before this device class is used
import c4_hooks

from c4_helpers import (
    C4_BUTTON_CLUSTER_ID,
    C4_CLUSTER_ID,
    C4_MANUF_CLUSTER,
    C4_PROFILE_BUTTON,
    C4_PROFILE_NETWORK,
    C4_PROFILE_OUTLET,
    C4ConfigCluster,
    C4DimmerManufCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import C4DualOutletButtonCluster
from c4_hooks import _C4_MODEL_QUIRK_MAP

# Reused UNCHANGED: outlet 0 now trusts the same real-ZCL transport as the
# C4-APD120 instead of a synthesized c4.dm.tv command (see module docstring).
from control4_dimmer import C4DimmerOnOff, C4DimmerLevelControl
# Reused UNCHANGED: outlet 1 has no real endpoint, so it keeps the
# confirmed-working on/off-only c4.dm.tv transport from control4_outlet.py.
from control4_outlet import C4Outlet1OnOff, C4OutletStateCluster

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Device quirk
# ---------------------------------------------------------------------------

class Control4LOZ5D1WDimmer(CustomDevice):
    """Control4 LOZ-5D1-W Dimming Outlet.

    Outlet 0 (EP1) is a dimmable light using real ZCL Level Control, mirroring
    the C4-APD120. Outlet 1 (synthetic EP11) is a plain on/off switch — see
    the module docstring for why it can't be a dimmer in this attempt.
    """

    signature = {
        "manufacturer_code": 0x1040,
        MODELS_INFO: [
            ("Control4", "LOZ-5D1-W"),
            ("Control4", "loz-5d1-w"),
            (None, "LOZ-5D1-W"),
            (None, "loz-5d1-w"),
            ("Control4", None),
        ],
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0101,
                INPUT_CLUSTERS: [
                    Basic.cluster_id,
                    Identify.cluster_id,
                    Groups.cluster_id,
                    Scenes.cluster_id,
                    OnOff.cluster_id,
                    LevelControl.cluster_id,
                    Time.cluster_id,
                ],
                OUTPUT_CLUSTERS: [],
            },
            2: {
                PROFILE_ID: C4_PROFILE_NETWORK,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
            196: {
                PROFILE_ID: C4_PROFILE_NETWORK,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID: C4_PROFILE_BUTTON,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
            # EP 198 is the key discriminator on the LOZ-5S1-W — assumed
            # present here too (still unverified for this model).
            198: {
                PROFILE_ID: C4_PROFILE_OUTLET,
                DEVICE_TYPE: 0x0101,
                INPUT_CLUSTERS:  [Basic.cluster_id],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    replacement = {
        SKIP_CONFIGURATION: True,
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0101,   # Dimmable light — real ZCL passthrough
                INPUT_CLUSTERS: [
                    C4BasicCluster,
                    Identify.cluster_id,
                    Groups.cluster_id,
                    Scenes.cluster_id,
                    C4DimmerOnOff,
                    C4DimmerLevelControl,
                    C4DimmerManufCluster,
                ],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            11: {                       # synthetic EP for outlet 2 — on/off only
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0100,
                INPUT_CLUSTERS: [
                    C4Outlet1OnOff,
                ],
                OUTPUT_CLUSTERS: [],
            },
            2: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4ConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            196: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4ConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4DualOutletButtonCluster],
                OUTPUT_CLUSTERS: [],
            },
            198: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4OutletStateCluster],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    device_automation_triggers = {
        ("click",   "outlet_1"): {COMMAND: "click",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        ("press",   "outlet_1"): {COMMAND: "press",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        ("release", "outlet_1"): {COMMAND: "release", CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        ("click",   "outlet_2"): {COMMAND: "click",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        ("press",   "outlet_2"): {COMMAND: "press",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        ("release", "outlet_2"): {COMMAND: "release", CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
    }


# ---------------------------------------------------------------------------
# Self-register with the get_device patch
# ---------------------------------------------------------------------------
for _c4_alias in (
    "loz-5d1-w", "LOZ-5D1-W", "C4-loz-5d1-w", "C4-LOZ-5D1-W",
):
    _C4_MODEL_QUIRK_MAP[_c4_alias] = Control4LOZ5D1WDimmer
_LOGGER.warning(
    "C4 LOZ-5D1-W: registered dimmer aliases (attempt 3 - real ZCL passthrough)"
)
_LOGGER.info("C4 LOZ-5D1-W: registered loz-5d1-w in _C4_MODEL_QUIRK_MAP")
