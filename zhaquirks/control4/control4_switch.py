"""ZHA quirk for the Control4 C4-SW120 On/Off Wall Switch."""

import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.profiles import zha
from zigpy.quirks import CustomCluster
from zigpy.quirks.v2 import EntityType, QuirkBuilder
from zigpy.zcl import foundation
from zigpy.zcl.foundation import Status as ZCLStatus
from zigpy.zcl.clusters.general import Basic, BinaryInput, Groups, OnOff, Scenes
from zigpy.zcl.clusters.lighting import Color

from zhaquirks.const import (
    CLUSTER_ID,
    COMMAND,
    ENDPOINT_ID,
    LONG_PRESS,
    LONG_RELEASE,
    SHORT_PRESS,
)

# Ensure patches are installed before this quirk is registered
import c4_hooks

import c4_helpers as C4
from c4_helpers import (
    DIMMER_BUTTON_EVENT_EP_MAP,
    DIMMER_BUTTON_MAP,
    C4DimmerManufCluster,
    C4ConfigCluster,
    strip_c4_endpoint,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import C4SwitchButtonClusterWithBinarySensor, _DIMMER_BUTTON_CLUSTERS
from c4_attached_switch import (
    ATTACHED_SWITCH_EP_MAP,
    C4ButtonAttachedOnOff,
    C4LedAttachedOnOff,
)
from c4_led_rgb import (
    LED_COLOR_EP_MAP,
    LED_OFF_COLOR_EP_MAP,
    C4LedOnOff,
    C4LedLevelControl,
    C4TopLedColorCluster,
    C4BottomLedColorCluster,
    C4TopLedOffColorCluster,
    C4BottomLedOffColorCluster,
)
from c4_hooks import _C4_MODEL_QUIRK_MAP

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Switch-specific cluster
# ---------------------------------------------------------------------------

class C4SwitchOnOff(CustomCluster, OnOff):
    """OnOff cluster for C4 wall switches.

    Uses standard cluster 6 on/off commands (unlike dimmers which redirect
    through LevelControl).  Defaults expect_reply=False.
    """

    cluster_id = OnOff.cluster_id
    _SUCCESS   = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    async def command(
        self,
        command_id,
        *args,
        manufacturer=None,
        expect_reply=False,
        tsn=None,
        **kwargs,
    ):
        _LOGGER.debug(
            "C4 SwitchOnOff: cmd=%s expect_reply=%s", command_id, expect_reply
        )
        result = await super().command(
            command_id, *args,
            manufacturer=manufacturer, expect_reply=expect_reply,
            tsn=tsn, **kwargs,
        )
        return result if result is not None else self._SUCCESS


# ---------------------------------------------------------------------------
# Device quirk (QuirkBuilder v2)
#
# EP1 is real and answers a genuine Simple_Desc_req; only its cluster list
# changes (Identify/the 0xFFFF output cluster are left untouched).
#
# EP196/EP197 are real endpoints too — c4_hooks.py's Endpoint.initialize
# patch injects their profile/device_type/clusters at interview time,
# since they never answer Simple_Desc_req on the wire — but their sole
# real-wire cluster (C4_CLUSTER_ID / 0x0001) carries no useful ZCL schema.
# It's swapped for a ZHA-side-only virtual cluster (C4ConfigCluster /
# C4SwitchButtonClusterWithBinarySensor, both on manufacturer-specific IDs
# the real protocol never uses) and the endpoint's profile is forced from
# the C4 proprietary profile to the standard ZHA profile — ZHA only builds
# entities for clusters under the ZHA profile.
#
# EP2 does not exist on the wire at all for this model (absent from the
# original signature's ENDPOINTS) — it's purely a ZHA-side config endpoint.
#
# EP198/199 (per-button binary_sensor), 200/201 (button/led-attached
# switches) and 202-205 (per-button LED lights) mirror the LDZ-101 dimmer's
# own virtual endpoints exactly (same c4_attached_switch.py/c4_led_rgb.py
# classes, same endpoint numbers — no cross-device collision, since
# endpoint numbers are per physical device). UNCONFIRMED on real hardware
# for this specific model: the LDZ-101 (dimmer) and LOZ-5S1-W (outlet)
# both turned out to have a REAL EP198 already on the wire (profile
# 0xC25E, a Basic cluster) that the old CustomDevice-era quirks silently
# discarded — given how consistently that's shown up across every C4
# device checked so far, replaces_endpoint() is used for EP198 here too
# (instead of adds_endpoint()) since it safely handles either case: it
# reconfigures the endpoint if real, or creates it fresh if not — unlike
# adds_endpoint(), which assumes the endpoint doesn't exist yet and would
# silently do nothing on a real one, exactly the bug already hit and
# fixed on the dimmer.
# ---------------------------------------------------------------------------

_c4_sw120_entry = (
    QuirkBuilder(manufacturer="Control4", model="C4-SW120277")
    .also_applies_to("Control4", "LSZ-101")
    .also_applies_to("Control4", "LSZ-102")
    .also_applies_to("Control4", "C4-LSZ-101")
    .also_applies_to("Control4", "C4-LSZ-102")
    .skip_configuration()
    # --- EP1: real endpoint, cluster-level changes only ---
    .adds(C4BasicCluster, endpoint_id=1)
    .replaces(C4DimmerManufCluster, endpoint_id=1)
    .adds(Groups, endpoint_id=1)
    .adds(Scenes, endpoint_id=1)
    .adds(C4SwitchOnOff, endpoint_id=1)
    # --- EP2: ZHA-side-only virtual config endpoint (not on the wire) ---
    .adds_endpoint(2, profile_id=zha.PROFILE_ID, device_type=0x0000)
    .adds(C4ConfigCluster, endpoint_id=2)
)
# --- EP196/EP197: real endpoints, injected at interview time ---
_c4_sw120_entry = strip_c4_endpoint(_c4_sw120_entry, 196).adds(
    C4ConfigCluster, endpoint_id=196
)
_c4_sw120_entry = strip_c4_endpoint(_c4_sw120_entry, 197).adds(
    C4SwitchButtonClusterWithBinarySensor, endpoint_id=197
)

# Virtual per-button endpoints — one binary_sensor entity each in ZHA
# (press/release), hidden by default. See DIMMER_BUTTON_EVENT_EP_MAP /
# C4SwitchButtonClusterWithBinarySensor.
for _btn_ep_name, _btn_ep_id, _btn_label in (
    ("top", DIMMER_BUTTON_EVENT_EP_MAP["top"], "Button Top"),
    ("bottom", DIMMER_BUTTON_EVENT_EP_MAP["bottom"], "Button Bottom"),
):
    _c4_sw120_entry = (
        strip_c4_endpoint(_c4_sw120_entry, _btn_ep_id, remove_cluster_id=Basic.cluster_id)
        .adds(_DIMMER_BUTTON_CLUSTERS[_btn_ep_name], endpoint_id=_btn_ep_id)
        .change_entity_metadata(
            endpoint_id=_btn_ep_id,
            cluster_id=BinaryInput.cluster_id,
            new_fallback_name=_btn_label,
            new_entity_registry_enabled_default=False,
        )
    )

# Virtual button/led-attached endpoints — one Switch entity each in ZHA,
# moved into the device's Configuration section. See c4_attached_switch.py.
for _attach_name, _attach_cls, _attach_label in (
    ("button_attached", C4ButtonAttachedOnOff, "Button Attached"),
    ("led_attached", C4LedAttachedOnOff, "Led Attached"),
):
    _ep_id = ATTACHED_SWITCH_EP_MAP[_attach_name]
    _c4_sw120_entry = (
        strip_c4_endpoint(_c4_sw120_entry, _ep_id, remove_cluster_id=Basic.cluster_id)
        .adds(_attach_cls, endpoint_id=_ep_id)
        .change_entity_metadata(
            endpoint_id=_ep_id,
            cluster_id=OnOff.cluster_id,
            new_entity_category=EntityType.CONFIG,
            new_fallback_name=_attach_label,
        )
    )

# Virtual per-button LED-color/off-color endpoints — one RGB light entity
# each in ZHA, hidden by default. See c4_led_rgb.py.
for _ep_id, _color_cls, _led_label in (
    (LED_COLOR_EP_MAP["top"], C4TopLedColorCluster, "LED Top On"),
    (LED_COLOR_EP_MAP["bottom"], C4BottomLedColorCluster, "LED Bottom On"),
    (LED_OFF_COLOR_EP_MAP["top"], C4TopLedOffColorCluster, "LED Top Off"),
    (LED_OFF_COLOR_EP_MAP["bottom"], C4BottomLedOffColorCluster, "LED Bottom Off"),
):
    _c4_sw120_entry = (
        strip_c4_endpoint(
            _c4_sw120_entry, _ep_id,
            device_type=zha.DeviceType.COLOR_DIMMABLE_LIGHT,
            remove_cluster_id=Basic.cluster_id,
        )
        .adds(C4LedOnOff, endpoint_id=_ep_id)
        .adds(C4LedLevelControl, endpoint_id=_ep_id)
        .adds(_color_cls, endpoint_id=_ep_id)
        .change_entity_metadata(
            endpoint_id=_ep_id,
            cluster_id=Color.cluster_id,
            new_fallback_name=_led_label,
            new_entity_registry_enabled_default=False,
        )
    )

# CLUSTER_ID/ENDPOINT_ID point at DIMMER_BUTTON_EVENT_EP_MAP's virtual
# per-button endpoints, not at 197 (this cluster's own endpoint) — since
# C4SwitchButtonClusterWithBinarySensor fires zha_send_event there instead.
# Also corrects a stale trigger list: "click"/"release" never actually
# fired (nothing in DIMMER_EVENT_MAP produces those exact literal
# strings — the same dead-trigger bug already found and fixed on the
# dimmer), while the real actions (press/SHORT_PRESS/LONG_PRESS/
# LONG_RELEASE/DOUBLE_PRESS/TRIPLE_PRESS/QUADRUPLE_PRESS) were missing.
_c4_sw120_entry = (
    _c4_sw120_entry
    .device_automation_triggers(
        {
            (_action, _btn_name): {
                COMMAND: _action,
                CLUSTER_ID: BinaryInput.cluster_id,
                ENDPOINT_ID: DIMMER_BUTTON_EVENT_EP_MAP[_btn_name],
            }
            for _btn_id, _btn_name in DIMMER_BUTTON_MAP.items()
            # No DOUBLE/TRIPLE/QUADRUPLE_PRESS: clicks are reported at
            # release, one SHORT_PRESS each (C4ButtonCluster.CLICK_AT_RELEASE).
            for _action in ("press", SHORT_PRESS, LONG_PRESS, LONG_RELEASE)
        }
    )
    .add_to_registry()
)

# ---------------------------------------------------------------------------
# Self-register with the get_device patch
# ---------------------------------------------------------------------------
_C4_MODEL_QUIRK_MAP["C4-SW120277"] = _c4_sw120_entry
# The LSZ-101 / LSZ-102 in-wall switches speak the same proprietary protocol
# as the SW120277 and expose the standard cluster set on endpoint 1. They are
# on/off only (not dimmable), so they are mapped to the switch quirk, not the
# APD120 dimmer. Dispatch is by model string, so registering the aliases is
# sufficient - no separate signature is required.
for _c4_alias in (
    "LSZ-101", "LSZ-102",
    "C4-LSZ-101", "C4-LSZ-102",
):
    _C4_MODEL_QUIRK_MAP[_c4_alias] = _c4_sw120_entry
_LOGGER.info("C4 SW120277: registered LSZ-101 switch aliases")
_LOGGER.info("C4 SW120277: registered C4-SW120277 in _C4_MODEL_QUIRK_MAP")
