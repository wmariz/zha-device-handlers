"""Virtual OnOff-cluster switches for Control4 dimmer hardware-config toggles.

button_attached/led_attached (confirmed wire protocol: `c4.dm.ba <0|1>` /
`c4.dm.lm <0|1>` — see c4_ramp_cluster.py's module docstring for the full
capture) were first implemented as custom Bool attributes on C4RampCluster.
CONFIRMED BROKEN on real hardware: ZHA does not auto-generate any entity
for an arbitrary attribute on a fully custom, manufacturer-specific
cluster — only a handful of core-recognized ZCL attributes (e.g.
LevelControl's on_level/transition times) have hardcoded platform support
in ZHA itself. After reload, the device's Configuration section never
listed button_attached/led_attached, and the raw Attributes-tab UI showed
a plain text box for them, not a boolean toggle either.

Instead, each toggle gets its own virtual endpoint carrying nothing but a
plain OnOff cluster — the same virtual-endpoint trick already used for
this device's two button Event entities (see c4_button_cluster.py /
DIMMER_BUTTON_EVENT_EP_MAP). ZHA's switch-platform discovery creates a
Switch entity for any endpoint exposing a plain OnOff cluster generically
(unrelated to any custom-attribute mechanism), which is exactly the
"configuration entity" being asked for, and gives a real True/False
toggle in the UI for free. On/off/toggle commands on this cluster send
the confirmed c4.dm.ba / c4.dm.lm wire command instead of controlling
real power; there is no confirmed Get command, so the on_off attribute is
just a local cache of the last value sent (seeded to True, matching the
real device's observed factory default).

Exported:
  C4ButtonAttachedOnOff — virtual switch cluster: button_attached (c4.dm.ba)
  C4LedAttachedOnOff    — virtual switch cluster: led_attached    (c4.dm.lm)
  ATTACHED_SWITCH_EP_MAP — {"button_attached": ep_id, "led_attached": ep_id}
"""

import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.quirks import CustomCluster
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import OnOff
from zigpy.zcl.foundation import Status as ZCLStatus

from c4_helpers import C4_CLUSTER_ID, C4_PROFILE_BUTTON, _build_c4_frame, next_c4_seq

_LOGGER = logging.getLogger(__name__)

# button/led attached — one virtual switch endpoint each
ATTACHED_SWITCH_EP_MAP = {"button_attached": 200, "led_attached": 201}


class C4AttachedOnOff(CustomCluster, OnOff):
    """Base: on/off/toggle commands set a C4 hardware-attached flag.

    Subclasses set _C4_NAMESPACE (the c4.dm.* wire namespace) and
    _C4_LABEL (for logging).
    """

    cluster_id = OnOff.cluster_id
    _SUCCESS = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    _C4_NAMESPACE: str = ""
    _C4_LABEL: str = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._update_attribute(self.AttributeDefs.on_off.id, True)

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

        await self._send_attached_flag(new_state)
        self._update_attribute(self.AttributeDefs.on_off.id, new_state)
        return self._SUCCESS

    async def _send_attached_flag(self, attached: bool):
        """Send a `0s<seq> <namespace> <0|1>` hardware-attached toggle."""
        device = self.endpoint.device
        value = 1 if attached else 0
        seq = next_c4_seq(device)
        cmd = f"0s{seq:04x} {self._C4_NAMESPACE} {value}"

        _LOGGER.info(
            "C4 %s: setting attached=%d — cmd: %s", self._C4_LABEL, value, cmd,
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
                "C4 %s: failed to set attached=%d — %s", self._C4_LABEL, value, e,
            )


class C4ButtonAttachedOnOff(C4AttachedOnOff):
    """Virtual switch: physical button hardware attached (c4.dm.ba)."""

    _C4_NAMESPACE = "c4.dm.ba"
    _C4_LABEL = "button_attached"


class C4LedAttachedOnOff(C4AttachedOnOff):
    """Virtual switch: LED indicator hardware attached (c4.dm.lm)."""

    _C4_NAMESPACE = "c4.dm.lm"
    _C4_LABEL = "led_attached"
