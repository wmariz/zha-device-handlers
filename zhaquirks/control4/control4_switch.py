"""ZHA quirk for the Control4 C4-SW120 On/Off Wall Switch."""

import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.profiles import zha
from zigpy.quirks import CustomCluster
from zigpy.quirks.v2 import QuirkBuilder
from zigpy.zcl import foundation
from zigpy.zcl.foundation import Status as ZCLStatus
from zigpy.zcl.clusters.general import Groups, OnOff, Scenes

from zhaquirks.const import (
    CLUSTER_ID,
    COMMAND,
    DOUBLE_PRESS,
    TRIPLE_PRESS,
    QUADRUPLE_PRESS,
    ENDPOINT_ID,
)

# Ensure patches are installed before this quirk is registered
import c4_hooks

import c4_helpers as C4
from c4_helpers import (
    C4_BUTTON_CLUSTER_ID,
    DIMMER_BUTTON_MAP,
    C4DimmerManufCluster,
    C4ConfigCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import C4SwitchButtonCluster
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
# C4SwitchButtonCluster, both on manufacturer-specific IDs the real
# protocol never uses) and the endpoint's profile is forced from the C4
# proprietary profile to the standard ZHA profile, matching the original
# CustomDevice replacement dict — ZHA only builds entities for clusters
# under the ZHA profile.
#
# EP2 does not exist on the wire at all for this model (absent from the
# original signature's ENDPOINTS) — it's purely a ZHA-side config
# endpoint, hence adds_endpoint() instead of replaces_endpoint().
# ---------------------------------------------------------------------------

_c4_sw120_trigger_actions = ("click", "press", "release", DOUBLE_PRESS, TRIPLE_PRESS, QUADRUPLE_PRESS)

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
    # --- EP196: real endpoint, injected at interview time ---
    .replaces_endpoint(196, profile_id=zha.PROFILE_ID, device_type=0x0000)
    .removes(C4.C4_CLUSTER_ID, endpoint_id=196)
    .adds(C4ConfigCluster, endpoint_id=196)
    # --- EP197: real endpoint, injected at interview time ---
    .replaces_endpoint(197, profile_id=zha.PROFILE_ID, device_type=0x0000)
    .removes(C4.C4_CLUSTER_ID, endpoint_id=197)
    .adds(C4SwitchButtonCluster, endpoint_id=197)
    .device_automation_triggers(
        {
            (_action, _btn_name): {
                COMMAND: _action,
                CLUSTER_ID: C4_BUTTON_CLUSTER_ID,
                ENDPOINT_ID: 197,
            }
            for _btn_id, _btn_name in DIMMER_BUTTON_MAP.items()
            for _action in _c4_sw120_trigger_actions
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
