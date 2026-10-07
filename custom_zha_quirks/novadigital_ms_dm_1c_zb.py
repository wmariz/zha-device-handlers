"""Nova Digital MS-DM-1C-ZB - Zigbee in-wall dimmer module (Tuya TS0601).

Reports as TS0601 / _TZE28C1000000_huu3td85. Not covered by zha-quirks nor by
zigbee-herdsman-converters (2026-10). It is a stock Tuya in-wall dimmer, so
this reuses the zha-quirks Tuya dimmer clusters. Datapoints (same as the Nova
Digital Topazio TO-DM dimmer in zigbee2mqtt, confirmed on the device for 1, 2
and 6):
    DP 1  on/off                     DP 4  light type (0 led, 1 incandescent, 2 halogen)
    DP 2  brightness 0-1000          DP 6  countdown (s)
    DP 3  minimum brightness 10-1000 (exposed here as 1-100 %)
The module's label shows L1/L2 and S1/S2, but it is sold as 1 channel; only
channel 1 is exposed. The wall switch on S1 drives the load locally and the
module reports the new state through DP 1/DP 2 (no separate input entity).

The "NM" (no manufacturer code) variants are used, as for other recent Tuya
dimmers whose firmware rejects frames carrying a manufacturer code.
"""

from zigpy.profiles import zha

from zhaquirks.builder import QuirkBuilder
from zhaquirks.tuya.mcu import (
    DPToAttributeMapping,
    TuyaLevelControlManufCluster,
    TuyaOnOffNM,
)
from zhaquirks.tuya.ts0601_dimmer import TuyaInWallLevelControlNM


class NovaDigitalDimmerLevelControl(TuyaInWallLevelControlNM):
    """Level cluster; minimum_level holds the minimum brightness in %."""


class NovaDigitalDimmerManufCluster(TuyaLevelControlManufCluster):
    """Tuya MCU cluster with DP 3 (minimum brightness) mapped to 1-100 %."""

    dp_to_attribute = dict(TuyaLevelControlManufCluster.dp_to_attribute)
    dp_to_attribute[3] = DPToAttributeMapping(
        NovaDigitalDimmerLevelControl.ep_attribute,
        "minimum_level",
        # device range 10-1000 (0.1 % steps) <-> 1-100 %
        converter=lambda x: max(1, min(100, round(x / 10))),
        dp_converter=lambda x: max(10, min(1000, int(x) * 10)),
    )


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
    .add_to_registry()
)
