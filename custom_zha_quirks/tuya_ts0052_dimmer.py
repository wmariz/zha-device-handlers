"""Tuya TS0052 1-channel Zigbee dimmer module (_TZ3000_mr2qqt8j).

Plain ZCL device (OnOff/LevelControl, not a Tuya MCU TS0601). Without a quirk
ZHA only exposes the light. zigbee2mqtt ("TS0052" generic definition,
tuyaLight with powerOnBehavior / switchType / minBrightness: "attribute")
also exposes:
    power_on_behavior   0xE001 / 0xD010 enum8 (0 off, 1 on, 2 previous)
    switch_type         0xE001 / 0xD030 enum8 (0 toggle, 1 state, 2 momentary)
    min_brightness      0x0008 / 0xFC00 uint16: high byte = min, low byte = max
                        (z2m always writes max = 0xFF)
This quirk exposes those, plus the max brightness byte of 0xFC00. 0xFC00 is
split into two virtual attributes in % (min_brightness / max_brightness);
writing either one writes the combined 0xFC00 value.

Also defined (no entities) only so they can be probed: OnOff 0x8002
(power_on_state on other Tuya switches), 0xE001/0xD020 switch_mode and
LevelControl 0xFC02-0xFC04 (TS110E bulb type / min / max).
"""

from typing import Final

from zigpy import types as t
from zigpy.quirks import CustomCluster
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import LevelControl, OnOff
from zigpy.zcl.foundation import BaseAttributeDefs, ZCLAttributeDef

from zhaquirks.builder import QuirkBuilder
from zhaquirks.tuya import TUYA_CLUSTER_E001_ID, PowerOnState


class SwitchType(t.enum8):
    """External (wall) switch type."""

    Toggle = 0x00
    State = 0x01
    Momentary = 0x02


class TS0052OnOff(CustomCluster, OnOff):
    """OnOff with the Tuya power-on attribute (probe only)."""

    class AttributeDefs(OnOff.AttributeDefs):
        """Attribute definitions."""

        power_on_state: Final = ZCLAttributeDef(id=0x8002, type=PowerOnState)


class TS0052E001(CustomCluster):
    """Tuya 0xE001 cluster: power-on behavior and switch type."""

    name = "Tuya E001"
    cluster_id = TUYA_CLUSTER_E001_ID
    ep_attribute = "tuya_e001"

    class AttributeDefs(BaseAttributeDefs):
        """Attribute definitions."""

        power_on_behavior: Final = ZCLAttributeDef(id=0xD010, type=PowerOnState)
        switch_mode: Final = ZCLAttributeDef(id=0xD020, type=t.enum8)
        switch_type: Final = ZCLAttributeDef(id=0xD030, type=SwitchType)


_MIN_MAX = 0xFC00
_VIRT_MIN = 0xEF10
_VIRT_MAX = 0xEF11


def _raw_to_pct(raw: int) -> int:
    return max(1, min(100, round(raw * 100 / 255)))


def _pct_to_raw(pct: int) -> int:
    return max(1, min(255, round(int(pct) * 255 / 100)))


class TS0052LevelControl(CustomCluster, LevelControl):
    """LevelControl with the Tuya min/max brightness attribute 0xFC00."""

    class AttributeDefs(LevelControl.AttributeDefs):
        """Attribute definitions."""

        tuya_min_max: Final = ZCLAttributeDef(id=_MIN_MAX, type=t.uint16_t)
        bulb_type: Final = ZCLAttributeDef(id=0xFC02, type=t.enum8)
        tuya_min_level: Final = ZCLAttributeDef(id=0xFC03, type=t.uint16_t)
        tuya_max_level: Final = ZCLAttributeDef(id=0xFC04, type=t.uint16_t)
        # virtual, in %, derived from 0xFC00
        min_brightness: Final = ZCLAttributeDef(id=_VIRT_MIN, type=t.uint8_t)
        max_brightness: Final = ZCLAttributeDef(id=_VIRT_MAX, type=t.uint8_t)

    def _update_attribute(self, attrid, value):
        super()._update_attribute(attrid, value)
        if attrid == _MIN_MAX and value is not None:
            super()._update_attribute(_VIRT_MIN, _raw_to_pct(value >> 8))
            super()._update_attribute(_VIRT_MAX, _raw_to_pct(value & 0xFF))

    async def write_attributes(self, attributes, manufacturer=None, **kwargs):
        """Fold writes of the virtual min/max into one 0xFC00 write."""
        real, virt = {}, {}
        for key, value in attributes.items():
            attr_id = self.find_attribute(key).id
            if attr_id in (_VIRT_MIN, _VIRT_MAX):
                virt[attr_id] = int(value)
            else:
                real[key] = value
        if virt:
            lo = virt.get(_VIRT_MIN, self.get(_VIRT_MIN) or 1)
            hi = virt.get(_VIRT_MAX, self.get(_VIRT_MAX) or 100)
            real[_MIN_MAX] = (_pct_to_raw(lo) << 8) | _pct_to_raw(max(lo, hi))
        return await super().write_attributes(real, manufacturer, **kwargs)

    async def read_attributes(self, attributes, *args, **kwargs):
        """Serve the virtual min/max by reading 0xFC00 from the device."""
        real, virt = [], []
        for attr in attributes:
            if self.find_attribute(attr).id in (_VIRT_MIN, _VIRT_MAX):
                virt.append(attr)
            else:
                real.append(attr)
        if virt and not any(self.find_attribute(a).id == _MIN_MAX for a in real):
            real.append(_MIN_MAX)
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


(
    QuirkBuilder("_TZ3000_mr2qqt8j", "TS0052")
    .replaces(TS0052OnOff)
    .replaces(TS0052LevelControl)
    .replaces(TS0052E001)
    .number(
        "min_brightness",
        LevelControl.cluster_id,
        min_value=1,
        max_value=100,
        step=1,
        unit="%",
        mode="box",
        translation_key="minimum_brightness",
        fallback_name="Minimum brightness",
    )
    .number(
        "max_brightness",
        LevelControl.cluster_id,
        min_value=1,
        max_value=100,
        step=1,
        unit="%",
        mode="box",
        translation_key="maximum_brightness",
        fallback_name="Maximum brightness",
    )
    .enum(
        "power_on_behavior",
        PowerOnState,
        TUYA_CLUSTER_E001_ID,
        translation_key="power_on_behavior",
        fallback_name="Power on behavior",
    )
    .enum(
        "switch_type",
        SwitchType,
        TUYA_CLUSTER_E001_ID,
        translation_key="switch_type",
        fallback_name="Switch type",
    )
    .add_to_registry()
)
