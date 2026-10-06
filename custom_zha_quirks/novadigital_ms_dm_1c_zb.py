"""Nova Digital MS-DM-1C-ZB - Zigbee in-wall dimmer module (Tuya TS0601).

Reports as TS0601 / _TZE28C1000000_huu3td85. Not covered by zha-quirks nor by
zigbee-herdsman-converters (2026-10). Its signature only differs from the stock
Tuya in-wall dimmers by extra clusters (0x0003, 0xE000, 0xEB00, 0xED00) and the
Green Power endpoint, so this reuses the stock Tuya dimmer clusters, whose
datapoints match the Nova Digital Topazio TO-DM dimmer in zigbee2mqtt:
    DP 1  on/off            DP 3  minimum brightness (0-1000)
    DP 2  brightness 0-1000 DP 4  light type (0 led, 1 incandescent, 2 halogen)
The module's label shows L1/L2 and S1/S2, but it is sold as 1 channel; only
channel 1 is exposed here.

The "NM" (no manufacturer code) variants are used, as for other recent Tuya
dimmers whose firmware rejects frames carrying a manufacturer code.
"""

from zigpy.profiles import zgp, zha
from zigpy.zcl.clusters.general import (
    Basic,
    GreenPowerProxy,
    Groups,
    Identify,
    Ota,
    Scenes,
    Time,
)

from zhaquirks.const import (
    DEVICE_TYPE,
    ENDPOINTS,
    INPUT_CLUSTERS,
    MODELS_INFO,
    OUTPUT_CLUSTERS,
    PROFILE_ID,
)
from zhaquirks.tuya import TUYA_CLUSTER_E000_ID, TUYA_CLUSTER_ED00_ID, TuyaDimmerSwitch
from zhaquirks.tuya.mcu import TuyaLevelControlManufCluster, TuyaOnOffNM
from zhaquirks.tuya.ts0601_dimmer import TuyaInWallLevelControlNM

TUYA_CLUSTER_EB00_ID = 0xEB00


class NovaDigitalMSDM1CZB(TuyaDimmerSwitch):
    """Nova Digital MS-DM-1C-ZB in-wall dimmer module."""

    signature = {
        MODELS_INFO: [("_TZE28C1000000_huu3td85", "TS0601")],
        ENDPOINTS: {
            # <SimpleDescriptor endpoint=1 profile=260 device_type=0x0051
            # input_clusters=[0, 3, 4, 5, 57344, 60160, 60672, 61184]
            # output_clusters=[10, 25]>
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: zha.DeviceType.SMART_PLUG,
                INPUT_CLUSTERS: [
                    Basic.cluster_id,
                    Identify.cluster_id,
                    Groups.cluster_id,
                    Scenes.cluster_id,
                    TUYA_CLUSTER_E000_ID,
                    TUYA_CLUSTER_EB00_ID,
                    TUYA_CLUSTER_ED00_ID,
                    TuyaLevelControlManufCluster.cluster_id,
                ],
                OUTPUT_CLUSTERS: [Time.cluster_id, Ota.cluster_id],
            },
            # <SimpleDescriptor endpoint=242 profile=41440 device_type=97
            # input_clusters=[] output_clusters=[33]>
            242: {
                PROFILE_ID: zgp.PROFILE_ID,
                DEVICE_TYPE: zgp.DeviceType.PROXY_BASIC,
                INPUT_CLUSTERS: [],
                OUTPUT_CLUSTERS: [GreenPowerProxy.cluster_id],
            },
        },
    }

    replacement = {
        ENDPOINTS: {
            1: {
                DEVICE_TYPE: zha.DeviceType.DIMMABLE_LIGHT,
                INPUT_CLUSTERS: [
                    Basic.cluster_id,
                    Identify.cluster_id,
                    Groups.cluster_id,
                    Scenes.cluster_id,
                    TUYA_CLUSTER_E000_ID,
                    TUYA_CLUSTER_EB00_ID,
                    TUYA_CLUSTER_ED00_ID,
                    TuyaLevelControlManufCluster,
                    TuyaOnOffNM,
                    TuyaInWallLevelControlNM,
                ],
                OUTPUT_CLUSTERS: [Time.cluster_id, Ota.cluster_id],
            },
            242: {
                PROFILE_ID: zgp.PROFILE_ID,
                DEVICE_TYPE: zgp.DeviceType.PROXY_BASIC,
                INPUT_CLUSTERS: [],
                OUTPUT_CLUSTERS: [GreenPowerProxy.cluster_id],
            },
        }
    }
