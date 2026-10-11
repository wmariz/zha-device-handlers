"""Tuya TS0052 1-channel Zigbee dimmer module (_TZ3000_mr2qqt8j).

Plain ZCL device (OnOff/LevelControl, not a Tuya MCU TS0601). Without a quirk
ZHA only exposes the light. zigbee2mqtt ("TS0052" generic definition,
tuyaLight with powerOnBehavior / switchType / minBrightness: "attribute")
also exposes power-on behavior, switch type and min brightness.

Probed on this device (2026-10-11):
    0xE001 / 0xD010 power_on_behavior  enum8 (0 off, 1 on, 2 previous)  OK
    0xE001 / 0xD030 switch_type        enum8 (0 toggle, 1 state, 2 momentary)  OK
    0x0008 / 0xFC00 (z2m min brightness) read AND write -> UNSUPPORTED_ATTRIBUTE
    0x0008 / 0x0002 min_level, 0x0003 max_level (standard, normally read-only)
        -> writable and persistent, so they are used for min/max brightness
    (OnOff 0x8002 mirrors D010; FC02-FC04 unsupported; neither is exposed)
min_level/max_level are exposed as virtual attributes in % (1-100).
"""

from typing import Final

from zigpy import types as t
from zigpy.quirks import CustomCluster
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import LevelControl
from zigpy.zcl.foundation import BaseAttributeDefs, ZCLAttributeDef

from zhaquirks.builder import QuirkBuilder
from zhaquirks.tuya import TUYA_CLUSTER_E001_ID, PowerOnState


class SwitchType(t.enum8):
    """External (wall) switch type."""

    Toggle = 0x00
    State = 0x01
    Momentary = 0x02


class TS0052E001(CustomCluster):
    """Tuya 0xE001 cluster: power-on behavior and switch type."""

    name = "Tuya E001"
    cluster_id = TUYA_CLUSTER_E001_ID
    ep_attribute = "tuya_e001"

    class AttributeDefs(BaseAttributeDefs):
        """Attribute definitions."""

        power_on_behavior: Final = ZCLAttributeDef(id=0xD010, type=PowerOnState)
        switch_type: Final = ZCLAttributeDef(id=0xD030, type=SwitchType)


_MIN_LEVEL = 0x0002
_MAX_LEVEL = 0x0003
_VIRT_MIN = 0xEF10
_VIRT_MAX = 0xEF11
# virtual (%) attribute -> real (1-254) attribute
_VIRTUAL = {_VIRT_MIN: _MIN_LEVEL, _VIRT_MAX: _MAX_LEVEL}
_REAL = {v: k for k, v in _VIRTUAL.items()}


def _level_to_pct(level: int) -> int:
    return max(1, min(100, round(level * 100 / 254)))


def _pct_to_level(pct: int) -> int:
    return max(1, min(254, round(int(pct) * 254 / 100)))


class TS0052LevelControl(CustomCluster, LevelControl):
    """LevelControl exposing the writable min/max level in %."""

    class AttributeDefs(LevelControl.AttributeDefs):
        """Attribute definitions."""

        min_brightness: Final = ZCLAttributeDef(id=_VIRT_MIN, type=t.uint8_t)
        max_brightness: Final = ZCLAttributeDef(id=_VIRT_MAX, type=t.uint8_t)

    def _update_attribute(self, attrid, value):
        super()._update_attribute(attrid, value)
        if attrid in _REAL and value is not None:
            super()._update_attribute(_REAL[attrid], _level_to_pct(value))

    async def write_attributes(self, attributes, manufacturer=None, **kwargs):
        """Write the virtual % attributes to min_level/max_level."""
        real = {}
        for key, value in attributes.items():
            attr_id = self.find_attribute(key).id
            if attr_id in _VIRTUAL:
                real[_VIRTUAL[attr_id]] = _pct_to_level(value)
            else:
                real[key] = value
        return await super().write_attributes(real, manufacturer, **kwargs)

    async def read_attributes(self, attributes, *args, **kwargs):
        """Serve the virtual % attributes by reading min_level/max_level."""
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


(
    QuirkBuilder("_TZ3000_mr2qqt8j", "TS0052")
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
