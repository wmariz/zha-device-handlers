"""Tuya TS110E 2-channel Zigbee dimmer module (_TZ3210_3mpwqzuu).

Plain ZCL device on endpoints 1 and 2. Without a quirk ZHA only exposes the
two lights; standard move_to_level_with_on_off works (verified 2026-10-29,
level 150 -> reported 0x0000 = 150, 0xF000 = 589), so brightness is left
untouched. zigbee2mqtt ("TS110E_2gang_2") also exposes, per channel:
    0x0008 / 0xFC02 switch type        uint8 (0 momentary, 1 toggle, 2 state)
    0x0008 / 0xFC03 min brightness     uint16 0-1000
    0x0008 / 0xFC04 max brightness     uint16 0-1000
    0x0006 / 0x8002 power-on behavior  uint8 (0 off, 1 on, 2 previous)
All four were reported by both endpoints after pairing (FC02 = 0,
FC03 = 11, FC04 = 1000, 8002 = 2). Min/max are exposed as virtual
attributes in % (1-100).
"""

from typing import Final

from zigpy import types as t
from zigpy.quirks import CustomCluster
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import LevelControl, OnOff
from zigpy.zcl.foundation import ZCLAttributeDef

from zhaquirks.builder import QuirkBuilder
from zhaquirks.tuya import PowerOnState


class SwitchType(t.enum8):
    """External (wall) switch type."""

    Momentary = 0x00
    Toggle = 0x01
    State = 0x02


_MIN_RAW = 0xFC03
_MAX_RAW = 0xFC04
_VIRT_MIN = 0xEF10
_VIRT_MAX = 0xEF11
# virtual (%) attribute -> real (0-1000) attribute
_VIRTUAL = {_VIRT_MIN: _MIN_RAW, _VIRT_MAX: _MAX_RAW}
_REAL = {v: k for k, v in _VIRTUAL.items()}


def _raw_to_pct(raw: int) -> int:
    return max(1, min(100, round(raw / 10)))


def _pct_to_raw(pct: int) -> int:
    return max(0, min(1000, int(pct) * 10))


class TS110ELevelControl(CustomCluster, LevelControl):
    """LevelControl with the Tuya switch type and min/max brightness."""

    class AttributeDefs(LevelControl.AttributeDefs):
        """Attribute definitions."""

        tuya_level: Final = ZCLAttributeDef(id=0xF000, type=t.uint16_t)
        switch_type: Final = ZCLAttributeDef(id=0xFC02, type=SwitchType)
        min_brightness_raw: Final = ZCLAttributeDef(id=_MIN_RAW, type=t.uint16_t)
        max_brightness_raw: Final = ZCLAttributeDef(id=_MAX_RAW, type=t.uint16_t)
        min_brightness: Final = ZCLAttributeDef(id=_VIRT_MIN, type=t.uint8_t)
        max_brightness: Final = ZCLAttributeDef(id=_VIRT_MAX, type=t.uint8_t)

    def _update_attribute(self, attrid, value):
        super()._update_attribute(attrid, value)
        if attrid in _REAL and value is not None:
            super()._update_attribute(_REAL[attrid], _raw_to_pct(value))

    async def write_attributes(self, attributes, manufacturer=None, **kwargs):
        """Write the virtual % attributes to 0xFC03/0xFC04."""
        real = {}
        for key, value in attributes.items():
            attr_id = self.find_attribute(key).id
            if attr_id in _VIRTUAL:
                real[_VIRTUAL[attr_id]] = _pct_to_raw(value)
            else:
                real[key] = value
        result = await super().write_attributes(real, manufacturer, **kwargs)
        # the device does not report the new value back on its own
        for attr_id, raw in real.items():
            if isinstance(attr_id, int) and attr_id in _REAL:
                self._update_attribute(attr_id, raw)
        return result

    async def read_attributes(self, attributes, *args, **kwargs):
        """Serve the virtual % attributes by reading 0xFC03/0xFC04."""
        real, virt = [], []
        for attr in attributes:
            attr_id = self.find_attribute(attr).id
            (virt if attr_id in _VIRTUAL else real).append(attr)
        for attr in virt:
            target = _VIRTUAL[self.find_attribute(attr).id]
            if not any(self.find_attribute(a).id == target for a in real):
                real.append(target)
        success, failure = (
            await super().read_attributes(real, *args, **kwargs) if real else ({}, {})
        )
        for attr in virt:
            value = self.get(self.find_attribute(attr).id)
            if value is None:
                failure[attr] = foundation.Status.UNSUPPORTED_ATTRIBUTE
            else:
                success[attr] = value
        return success, failure


class TS110EOnOff(CustomCluster, OnOff):
    """OnOff with the Tuya power-on behavior."""

    class AttributeDefs(OnOff.AttributeDefs):
        """Attribute definitions."""

        power_on_behavior: Final = ZCLAttributeDef(id=0x8002, type=PowerOnState)


builder = QuirkBuilder("_TZ3210_3mpwqzuu", "TS110E")
for _ep in (1, 2):
    builder = (
        builder.replaces(TS110ELevelControl, endpoint_id=_ep)
        .replaces(TS110EOnOff, endpoint_id=_ep)
        .number(
            "min_brightness",
            LevelControl.cluster_id,
            endpoint_id=_ep,
            min_value=1,
            max_value=100,
            step=1,
            unit="%",
            mode="box",
            translation_key=f"minimum_brightness_l{_ep}",
            fallback_name=f"Brilho mínimo (canal {_ep})",
            # virtual: read 0xFC03 at startup instead of trusting the cache
            attribute_initialized_from_cache=False,
        )
        .number(
            "max_brightness",
            LevelControl.cluster_id,
            endpoint_id=_ep,
            min_value=1,
            max_value=100,
            step=1,
            unit="%",
            mode="box",
            translation_key=f"maximum_brightness_l{_ep}",
            fallback_name=f"Brilho máximo (canal {_ep})",
            # virtual: read 0xFC04 at startup instead of trusting the cache
            attribute_initialized_from_cache=False,
        )
        .enum(
            "switch_type",
            SwitchType,
            LevelControl.cluster_id,
            endpoint_id=_ep,
            translation_key=f"switch_type_l{_ep}",
            fallback_name=f"Tipo de interruptor (canal {_ep})",
        )
        .enum(
            "power_on_behavior",
            PowerOnState,
            OnOff.cluster_id,
            endpoint_id=_ep,
            translation_key=f"power_on_behavior_l{_ep}",
            fallback_name=f"Ao ligar a energia (canal {_ep})",
        )
    )
builder.add_to_registry()
