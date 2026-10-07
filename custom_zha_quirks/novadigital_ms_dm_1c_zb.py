"""Nova Digital MS-DM-1C-ZB - Zigbee in-wall dimmer module (Tuya TS0601).

Reports as TS0601 / _TZE28C1000000_huu3td85. Not covered by zha-quirks nor by
zigbee-herdsman-converters (2026-10). It is a stock Tuya in-wall dimmer, so
this reuses the zha-quirks Tuya dimmer clusters. Datapoints (same as the Nova
Digital Topazio TO-DM dimmer in zigbee2mqtt; a data query on the device
returned DPs 1, 2, 3, 4, 5, 6 and 14):
    DP 1  on/off                     DP 4  light type (0 led, 1 incandescent, 2 halogen)
    DP 2  brightness 0-1000          DP 6  countdown (s)
    DP 3  minimum brightness 10-1000 (exposed as 1-100 %)
    DP 5  maximum brightness 10-1000 (exposed as 1-100 %)
    DP 14 power-on behavior (0 off, 1 on, 2 previous)
The module's label shows L1/L2 and S1/S2, but it is sold as 1 channel; only
channel 1 is exposed. The wall switch on S1 drives the load locally and the
module reports the new state through DP 1/DP 2 (no separate input entity).

The "NM" (no manufacturer code) variants are used, as for other recent Tuya
dimmers whose firmware rejects frames carrying a manufacturer code.
"""

from typing import Final

from zigpy.profiles import zha
import zigpy.types as t
from zigpy.zcl.foundation import ZCLAttributeDef

from zhaquirks.builder import QuirkBuilder
from zhaquirks.tuya.mcu import (
    DPToAttributeMapping,
    TuyaLevelControlManufCluster,
    TuyaOnOffNM,
)
from zhaquirks.tuya.ts0601_dimmer import TuyaInWallLevelControlNM


class LightType(t.enum8):
    """Connected load type (DP 4)."""

    LED = 0x00
    Incandescent = 0x01
    Halogen = 0x02


class PowerOnBehavior(t.enum8):
    """State after a power loss (DP 14)."""

    Off = 0x00
    On = 0x01
    Previous = 0x02


def _dp_to_pct(x):
    """Device 10-1000 (0.1 % steps) -> 1-100 %."""
    return max(1, min(100, round(x / 10)))


def _pct_to_dp(x):
    """1-100 % -> device 10-1000."""
    return max(10, min(1000, int(x) * 10))


class NovaDigitalDimmerLevelControl(TuyaInWallLevelControlNM):
    """Level cluster carrying the dimmer settings (min/max brightness in %)."""

    class AttributeDefs(TuyaInWallLevelControlNM.AttributeDefs):
        """Attribute definitions."""

        bulb_type: Final = ZCLAttributeDef(
            id=0xEF02, type=LightType, is_manufacturer_specific=True
        )
        maximum_level: Final = ZCLAttributeDef(
            id=0xEF03, type=t.uint32_t, is_manufacturer_specific=True
        )
        power_on_behavior: Final = ZCLAttributeDef(
            id=0xEF04, type=PowerOnBehavior, is_manufacturer_specific=True
        )


class NovaDigitalDimmerManufCluster(TuyaLevelControlManufCluster):
    """Tuya MCU cluster with the Nova Digital dimmer settings datapoints."""

    dp_to_attribute = dict(TuyaLevelControlManufCluster.dp_to_attribute)
    dp_to_attribute.update(
        {
            3: DPToAttributeMapping(
                NovaDigitalDimmerLevelControl.ep_attribute,
                "minimum_level",
                converter=_dp_to_pct,
                dp_converter=_pct_to_dp,
            ),
            4: DPToAttributeMapping(
                NovaDigitalDimmerLevelControl.ep_attribute,
                "bulb_type",
                converter=LightType,
            ),
            5: DPToAttributeMapping(
                NovaDigitalDimmerLevelControl.ep_attribute,
                "maximum_level",
                converter=_dp_to_pct,
                dp_converter=_pct_to_dp,
            ),
            14: DPToAttributeMapping(
                NovaDigitalDimmerLevelControl.ep_attribute,
                "power_on_behavior",
                converter=PowerOnBehavior,
            ),
        }
    )
    data_point_handlers = dict(TuyaLevelControlManufCluster.data_point_handlers)
    data_point_handlers.update({5: "_dp_2_attr_update", 14: "_dp_2_attr_update"})


(
    QuirkBuilder("_TZE28C1000000_huu3td85", "TS0601")
    .friendly_name(model="MS-DM-1C-ZB", manufacturer="Nova Digital")
    # expose EP 1 as a dimmable light (the device announces itself as a smart plug)
    .replaces_endpoint(1, profile_id=zha.PROFILE_ID, device_type=zha.DeviceType.DIMMABLE_LIGHT)
    .replaces(NovaDigitalDimmerManufCluster)
    .adds(TuyaOnOffNM)
    .adds(NovaDigitalDimmerLevelControl)
    .number(
        "minimum_level",
        NovaDigitalDimmerLevelControl.cluster_id,
        min_value=1,
        max_value=100,
        step=1,
        unit="%",
        mode="box",
        translation_key="minimum_brightness",
        fallback_name="Minimum brightness",
    )
    .number(
        "maximum_level",
        NovaDigitalDimmerLevelControl.cluster_id,
        min_value=1,
        max_value=100,
        step=1,
        unit="%",
        mode="box",
        translation_key="maximum_brightness",
        fallback_name="Maximum brightness",
    )
    .enum(
        "bulb_type",
        LightType,
        NovaDigitalDimmerLevelControl.cluster_id,
        translation_key="light_type",
        fallback_name="Light type",
    )
    .enum(
        "power_on_behavior",
        PowerOnBehavior,
        NovaDigitalDimmerLevelControl.cluster_id,
        translation_key="power_on_behavior",
        fallback_name="Power on behavior",
    )
    .add_to_registry()
)
