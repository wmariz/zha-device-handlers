"""Per-button "Keypad Managed" switch entities for the KPZ-6B1 keypad.

CONFIRMED from a real HC300 controller log: `0s<seq> c4.kp.llm <btn> <val>`
sets whether a given button's LED is under keypad-local management (the
"Keypad Managed" checkbox seen per-button in Composer). Note the wire
shape differs from c4.kp.lo/lf: the button index here is a single,
unpadded hex digit ("0".."5", not "00".."05"), while the value is
zero-padded to two digits ("00"/"01") — confirmed from six real captured
commands toggling every button.

"Follow Bound Color" (the OTHER per-button checkbox seen alongside
"Keypad Managed" in Composer) is NOT implemented here — it was never
toggled in the captured log (stayed True throughout), so there is no
confirmed wire command for it yet.

Mirrors c4_attached_switch.py's C4AttachedOnOff pattern exactly (virtual
OnOff-cluster endpoint -> real Switch entity), parameterized per button
via a factory, the same way c4_keypad_led_rgb.py parameterizes the LED
color cluster.

Exported:
  _make_keypad_managed_cluster() — factory for one button's managed-switch cluster
  _KEYPAD_MANAGED_CLUSTERS       — per-button virtual cluster dict (btn_id → class)
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

from c4_helpers import C4_CLUSTER_ID, C4_PROFILE_BUTTON, KPZ6B1_BUTTON_MAP, _build_c4_frame, next_c4_seq

_LOGGER = logging.getLogger(__name__)

# top/bottom-style per-button virtual endpoint map for the managed switch
KEYPAD_MANAGED_EP_MAP: dict[int, int] = {
    btn_id: 220 + btn_id for btn_id in KPZ6B1_BUTTON_MAP
}


class C4KeypadManagedOnOff(CustomCluster, OnOff):
    """OnOff cluster: on/off/toggle send the confirmed c4.kp.llm command.

    Subclasses (via _make_keypad_managed_cluster) set _BUTTON_IDX.
    """

    cluster_id = OnOff.cluster_id
    _SUCCESS = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    _BUTTON_IDX: int = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Seeded False to match button 0's observed factory default in the
        # captured log (<IsManaged>False</IsManaged>); other buttons were
        # already True by the time they were captured, so this is a best
        # guess for the true factory default, not independently confirmed
        # per-button.
        self._update_attribute(self.AttributeDefs.on_off.id, False)

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
            new_state = not bool(self.get(self.AttributeDefs.on_off.id, False))
        else:
            return await super().command(
                command_id, *args, manufacturer=manufacturer,
                expect_reply=expect_reply, tsn=tsn, **kwargs,
            )

        await self._send_managed_flag(new_state)
        self._update_attribute(self.AttributeDefs.on_off.id, new_state)
        return self._SUCCESS

    async def _send_managed_flag(self, managed: bool):
        """Send a `0s<seq> c4.kp.llm <btn> <00|01>` command.

        Button index is a single, unpadded hex digit; the value is
        zero-padded to two digits — CONFIRMED from six real captured
        commands, one per button.
        """
        device = self.endpoint.device
        value = 1 if managed else 0
        seq = next_c4_seq(device)
        cmd = f"0s{seq:04x} c4.kp.llm {self._BUTTON_IDX:x} {value:02x}"

        _LOGGER.info(
            "C4 keypad_managed button %d (endpoint %d): setting managed=%d "
            "— cmd: %s",
            self._BUTTON_IDX, self.endpoint.endpoint_id, value, cmd,
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
                "C4 keypad_managed button %d (endpoint %d): failed to set "
                "managed=%d — %s",
                self._BUTTON_IDX, self.endpoint.endpoint_id, value, e,
            )


def _make_keypad_managed_cluster(btn_id: int) -> type:
    """Return a unique C4KeypadManagedOnOff subclass for one button."""

    class _Cluster(C4KeypadManagedOnOff):
        _BUTTON_IDX = btn_id

    _Cluster.__name__     = f"C4Keypad{btn_id}ManagedOnOff"
    _Cluster.__qualname__ = _Cluster.__name__
    return _Cluster


# One switch cluster class per keypad button — keyed by button id (0-5)
_KEYPAD_MANAGED_CLUSTERS: dict[int, type] = {
    btn_id: _make_keypad_managed_cluster(btn_id) for btn_id in KPZ6B1_BUTTON_MAP
}
