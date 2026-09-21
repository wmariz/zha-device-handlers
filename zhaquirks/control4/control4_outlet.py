"""ZHA quirk for the Control4 LOZ-5S1-W Switched Outlet.

Hardware: dual switched outlet with physical on/off paddle button.

Zigbee endpoints after interview:
  1   — ZHA profile, device_type 0x0101, clusters [Basic … OnOff Level Time]
  2   — C4_PROFILE_NETWORK 0xC25D, cluster 0x0001 (injected by patch)
  196 — C4_PROFILE_NETWORK 0xC25D, cluster 0x0001 (injected by patch)
  197 — C4_PROFILE_BUTTON  0xC25C, cluster 0x0001 (injected by patch)
  198 — C4_PROFILE_OUTLET  0xC25E, cluster 0x0000 (auto-discovered)

EP 198 (profile 0xC25E) is absent on APD120/SW120, making it the reliable
signature discriminator.

Protocol (confirmed from Wireshark captures):
  Model string: "c4:outlet_switch:loz-5s1-w", firmware "03.19.49"
  Custom cluster 0xC25C with text-based ASCII commands over ZigBee APS.

  SET command (coordinator → device, C4_PROFILE_BUTTON EP 1→1):
    0s<seq4> c4.dm.tv <outlet> 00 <level>\r\n
    outlet: 00 = outlet index 0, 01 = outlet index 1
    level:  64 (hex 100) = ON, 00 = OFF

  RESPONSE (device → coordinator, EP 197):
    0r<seq4> 000

  STATE ANNOUNCE (device → coordinator, EP 197):
    0t<seq4> sa c4.dm.tc <outlet> <level>\r\n
    outlet: echoes the outlet index from the SET command
    level:  64 = ON, 00 = OFF

  SET→RESPONSE→ANNOUNCE cycle completes in ~200 ms.
  Coordinator sends SET 2–3× via different mesh relay paths for reliability.

  On rejoin the device broadcasts ZCL Report Attributes (cluster 0x005D)
  containing model/firmware; coordinator re-syncs state with SET commands.

Outlet-specific notes:
  • attr 0x0000 on EP 2 cluster 0x0001 is an on/off flag (not a dim level).
  • On/off commands use C4 serial protocol (c4.dm.tv), NOT standard ZCL OnOff.
"""

import asyncio
import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.profiles import zha
from zigpy.quirks import CustomCluster
from zigpy.quirks.v2 import ClusterType, QuirkBuilder
from zigpy.zcl import foundation
from zigpy.zcl.foundation import Status as ZCLStatus
from zigpy.zcl.clusters.general import (
    Basic, Groups, Identify, LevelControl, OnOff, Scenes, Time,
)

from zhaquirks.const import (
    CLUSTER_ID,
    COMMAND,
    ENDPOINT_ID,
)

# Ensure patches are installed before this quirk is registered
import c4_hooks

import c4_helpers as C4
from c4_helpers import (
    C4_ATTR_DIM_LEVEL,
    C4_ATTR_FIRMWARE,
    C4_ATTR_MODEL,
    C4_BUTTON_CLUSTER_ID,
    C4_CLUSTER_ID,
    C4_MANUF_CLUSTER,
    C4_PROFILE_BUTTON,
    C4_PROVISION_DELAY,
    OUTLET_EP_MAP,
    _INVALID_MODELS,
    _build_c4_frame,
    _sync_ep1_onoff,
    next_c4_seq,
    C4ConfigCluster,
    C4DimmerManufCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import C4DualOutletButtonCluster
from c4_hooks import _C4_MODEL_QUIRK_MAP

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Outlet-specific config cluster variant
# ---------------------------------------------------------------------------

class C4OutletConfigCluster(C4ConfigCluster):
    """C4 config cluster for outlet devices (EP 2 / EP 196).

    On a dimmer, attr 0x0000 on cluster 0x0001 is the dim level (0–255).
    On the LOZ-5S1-W the same attribute is an on/off flag (2=on, 0=off).
    Everything else is identical to C4ConfigCluster.
    """

    def _update_attribute(self, attrid, value):
        if attrid == C4_ATTR_DIM_LEVEL:
            is_on = isinstance(value, int) and value > 0
            _LOGGER.debug(
                "C4 outlet config (ep %s): on/off state = %s (raw=%r)",
                self.endpoint.endpoint_id, "on" if is_on else "off", value,
            )
            _sync_ep1_onoff(
                self.endpoint.device, is_on, "outlet_ep2_report"
            )
        else:
            super()._update_attribute(attrid, value)


# ---------------------------------------------------------------------------
# EP 198 operational state cluster
# ---------------------------------------------------------------------------

class C4OutletStateCluster(CustomCluster):
    """Operational state cluster on EP 198 of the LOZ-5S1-W (profile 0xC25E).

    Handles unsolicited Report Attributes and Read Attr Rsp from the device
    for model string, firmware version, and on/off state.
    """

    cluster_id   = Basic.cluster_id   # 0x0000 — matches EP 198 descriptor
    name         = "Control4 Outlet State"
    ep_attribute = "c4_outlet_state"
    _c4_custom_handler = True

    def _update_attribute(self, attrid, value):
        super()._update_attribute(attrid, value)

        if attrid == C4_ATTR_MODEL and isinstance(value, str):
            _LOGGER.debug("C4 outlet state: model = %r", value)
            device = self.endpoint.device
            if not device.model or device.model in ("", "unknown"):
                device.model = value

        elif attrid == C4_ATTR_FIRMWARE and isinstance(value, str):
            _LOGGER.info("C4 outlet state: firmware = %s", value)

        elif attrid == 0x0000:
            is_on = isinstance(value, int) and value > 0
            _LOGGER.debug(
                "C4 outlet state ep198: on/off = %s (raw=%r)",
                "on" if is_on else "off", value,
            )
            _sync_ep1_onoff(
                self.endpoint.device, is_on, "outlet_ep198_report"
            )
        else:
            _LOGGER.debug("C4 outlet state: attr 0x%04x = %r", attrid, value)

    def handle_message(self, hdr, args):
        _LOGGER.debug(
            "C4 outlet state ep198: cmd=0x%02x args_hex=%s",
            hdr.command_id if hdr else -1,
            args.hex() if isinstance(args, (bytes, bytearray)) else repr(args),
        )

        if hdr is None:
            _LOGGER.warning(
                "C4 outlet state ep198: no ZCL header, raw=%r", args
            )
            return

        if hdr.command_id in (0x01, 0x0A):
            try:
                remaining = args
                while remaining:
                    if hdr.command_id == 0x01:
                        # Read Attr Rsp: [attr_id(2)] [status(1)] [type(1)] [value]
                        rec, remaining = (
                            foundation.ReadAttributeRecord.deserialize(remaining)
                        )
                        if rec.status == foundation.Status.SUCCESS:
                            self._update_attribute(rec.attrid, rec.value.value)
                    else:
                        # Report Attributes: [attr_id(2)] [type(1)] [value]
                        attr, remaining = (
                            foundation.Attribute.deserialize(remaining)
                        )
                        self._update_attribute(attr.attrid, attr.value.value)
            except Exception as exc:
                _LOGGER.warning(
                    "C4 outlet state ep198: parse error cmd=0x%02x — %s",
                    hdr.command_id, exc,
                )
            return

        if hdr.command_id == 0x00:
            _LOGGER.debug(
                "C4 outlet state ep198: ignoring Read Attributes from device"
            )
            return

        super().handle_cluster_request(hdr, args)

    async def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None,
    ):
        result = {}
        for attr in attributes:
            if isinstance(attr, str):
                try:
                    attr_id = self.find_attribute(attr).id
                except KeyError:
                    _LOGGER.debug(
                        "C4 OutletState ep198: unknown attr name %r, skipping", attr
                    )
                    continue
            else:
                attr_id = attr
            result[attr_id] = self._attr_cache.get(attr_id)
        return result, {}


# ---------------------------------------------------------------------------
# Outlet OnOff clusters
# ---------------------------------------------------------------------------

class C4OutletOnOff(CustomCluster, OnOff):
    """OnOff cluster for the LOZ-5S1-W outlet.

    Sends on/off commands using the C4 serial protocol:
      0s<seq4> c4.dm.tv <outlet_idx> 00 <level>\r\n
    on C4_PROFILE_BUTTON (0xC25C), EP 1→1.

    OUTLET_IDX is the C4 protocol outlet selector (00 or 01).
    Subclass overrides OUTLET_IDX for the second outlet.
    """

    cluster_id = OnOff.cluster_id
    OUTLET_IDX = 0
    _SUCCESS   = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    async def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None,
    ):
        """Poll outlet state using C4 protocol instead of ZCL Read Attributes.

        Sends a C4 Get command: 0g<seq4> c4.dm.tv <outlet_idx> 00
        The device responds with a c4.dm.tc state announcement which is
        handled by C4DualOutletButtonCluster._handle_state_announcement.
        Returns cached values to the caller.
        """
        if not only_cache:
            await self._poll_c4_outlet_state()

        result = {}
        for attr in attributes:
            if isinstance(attr, str):
                try:
                    attr_id = self.find_attribute(attr).id
                except KeyError:
                    continue
            else:
                attr_id = attr
            cached = self._attr_cache.get(attr_id)
            if cached is not None:
                result[attr_id] = cached
        return result, {}

    async def _poll_c4_outlet_state(self) -> None:
        """Send a C4 Get command to query the outlet's current state."""
        device = self.endpoint.device
        seq = next_c4_seq(device)
        cmd = f"0g{seq:04x} c4.dm.tv {self.OUTLET_IDX:02x} 00"
        data = _build_c4_frame(0, cmd)

        _LOGGER.debug("C4 OutletOnOff: polling outlet %d — %s", self.OUTLET_IDX, cmd)
        try:
            await device.request(
                profile=C4_PROFILE_BUTTON,
                cluster=C4_CLUSTER_ID,
                src_ep=1, dst_ep=1,
                sequence=device.get_sequence(),
                data=data,
                expect_reply=False,
            )
        except Exception as exc:
            _LOGGER.warning(
                "C4 OutletOnOff: poll outlet %d failed: %s", self.OUTLET_IDX, exc
            )

    async def _send_c4_outlet_command(self, is_on: bool) -> None:
        """Send c4.dm.tv <outlet> 00 <level> on C4_PROFILE_BUTTON, EP 1→1.

        Command format (confirmed from capture):
          0s<seq4> c4.dm.tv <outlet2> 00 <level2>\r\n
        where outlet is the 2-digit hex outlet index and level is 64 (ON)
        or 00 (OFF).
        """
        device = self.endpoint.device
        seq = next_c4_seq(device)
        level = 0x64 if is_on else 0x00
        cmd = f"0s{seq:04x} c4.dm.tv {self.OUTLET_IDX:02x} 00 {level:02x}"
        data = _build_c4_frame(0, cmd)

        _LOGGER.debug("C4 OutletOnOff: sending %s", cmd)
        try:
            await device.request(
                profile=C4_PROFILE_BUTTON,
                cluster=C4_CLUSTER_ID,
                src_ep=1, dst_ep=1,
                sequence=device.get_sequence(),
                data=data,
                expect_reply=False,
            )
        except Exception as exc:
            _LOGGER.warning("C4 OutletOnOff: c4.dm.tv send failed: %s", exc)

    async def command(
        self,
        command_id,
        *args,
        manufacturer=None,
        expect_reply=False,
        tsn=None,
        **kwargs,
    ):
        if command_id == OnOff.ServerCommandDefs.toggle.id:
            cached = self.get("on_off")
            command_id = (
                OnOff.ServerCommandDefs.off.id
                if cached else OnOff.ServerCommandDefs.on.id
            )

        if command_id in (
            OnOff.ServerCommandDefs.on.id,
            OnOff.ServerCommandDefs.off.id,
        ):
            is_on = command_id == OnOff.ServerCommandDefs.on.id
            await self._send_c4_outlet_command(is_on)
            # Optimistic update — device will confirm via c4.dm.tc announce
            self._update_attribute(OnOff.AttributeDefs.on_off.id, is_on)
            return self._SUCCESS

        _LOGGER.debug("C4 OutletOnOff: unhandled cmd=%s, passing through", command_id)
        result = await super().command(
            command_id, *args,
            manufacturer=manufacturer, expect_reply=expect_reply,
            tsn=tsn, **kwargs,
        )
        return result if result is not None else self._SUCCESS


class C4Outlet1OnOff(C4OutletOnOff):
    """OnOff cluster for the second outlet (outlet 1 / EP 11).

    Same C4 serial protocol as C4OutletOnOff but with OUTLET_IDX=1,
    so commands target the second physical outlet:
      0s<seq4> c4.dm.tv 01 00 <level>\r\n
    """

    OUTLET_IDX = 1


# ---------------------------------------------------------------------------
# Device quirk (QuirkBuilder v2)
#
# EP1, EP2, EP196, EP197, EP198 are all real, normally-interviewed endpoints
# (EP2/196/197 populated by c4_hooks.py's Endpoint.initialize patch, since
# they never answer Simple_Desc_req; EP198 answers for real and is the
# reliable signature discriminator for this model — see module docstring).
# Their profile/device_type is forced to the standard ZHA profile and their
# one real wire cluster is swapped for the ZHA-side virtual cluster, exactly
# matching the original CustomDevice replacement dict. EP11 (second outlet)
# never existed in the old signature at all — same "declared only in
# replacement" virtual-endpoint pattern used elsewhere in this fork.
# ---------------------------------------------------------------------------

_c4_loz5s1w_entry = (
    QuirkBuilder(manufacturer="Control4", model="loz-5s1-w")
    .skip_configuration()
    # --- EP1: real endpoint, device_type AND clusters change ---
    .replaces_endpoint(1, profile_id=zha.PROFILE_ID, device_type=0x0100)
    .replaces(C4BasicCluster, endpoint_id=1)
    .replaces(C4OutletOnOff, endpoint_id=1)
    .removes(LevelControl.cluster_id, endpoint_id=1)
    .removes(Time.cluster_id, endpoint_id=1)
    .adds(C4DimmerManufCluster, endpoint_id=1)
    .adds(C4_MANUF_CLUSTER, cluster_type=ClusterType.Client, endpoint_id=1)
    # --- EP11: synthetic endpoint for the second outlet (not on the wire) ---
    .adds_endpoint(11, profile_id=zha.PROFILE_ID, device_type=0x0100)
    .adds(C4Outlet1OnOff, endpoint_id=11)
    # --- EP2: real endpoint, injected at interview time ---
    .replaces_endpoint(2, profile_id=zha.PROFILE_ID, device_type=0x0000)
    .removes(C4.C4_CLUSTER_ID, endpoint_id=2)
    .adds(C4OutletConfigCluster, endpoint_id=2)
    # --- EP196: real endpoint, injected at interview time ---
    .replaces_endpoint(196, profile_id=zha.PROFILE_ID, device_type=0x0000)
    .removes(C4.C4_CLUSTER_ID, endpoint_id=196)
    .adds(C4OutletConfigCluster, endpoint_id=196)
    # --- EP197: real endpoint, injected at interview time ---
    .replaces_endpoint(197, profile_id=zha.PROFILE_ID, device_type=0x0000)
    .removes(C4.C4_CLUSTER_ID, endpoint_id=197)
    .adds(C4DualOutletButtonCluster, endpoint_id=197)
    # --- EP198: real endpoint, the model discriminator (see docstring) ---
    .replaces_endpoint(198, profile_id=zha.PROFILE_ID, device_type=0x0000)
    .replaces(C4OutletStateCluster, endpoint_id=198)
    .device_automation_triggers(
        {
            ("click",   "outlet_1"): {COMMAND: "click",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
            ("press",   "outlet_1"): {COMMAND: "press",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
            ("release", "outlet_1"): {COMMAND: "release", CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
            ("click",   "outlet_2"): {COMMAND: "click",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
            ("press",   "outlet_2"): {COMMAND: "press",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
            ("release", "outlet_2"): {COMMAND: "release", CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        }
    )
    .add_to_registry()
)


# ---------------------------------------------------------------------------
# Self-register with the get_device patch
# ---------------------------------------------------------------------------
_C4_MODEL_QUIRK_MAP["loz-5s1-w"] = _c4_loz5s1w_entry
_LOGGER.info("C4 LOZ-5S1-W: registered loz-5s1-w in _C4_MODEL_QUIRK_MAP")
# LOZ-5D1-W (dimmer variant) has its own quirk — see
# control4_outlet_dimmer.py — and is no longer aliased to this switch-only
# class.
