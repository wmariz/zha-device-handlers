"""Tuya ZY-M100-24GV3 24 GHz mmWave presence sensor (TS0601 _TZE204_gkfbdvyx).

Overrides the built-in zhaquirks.tuya.tuya_motion quirk for this device
(custom quirks register later and win in zha's DeviceRegistry). Compared to
the built-in one, following zigbee2mqtt's "ZY-M100-24GV3" definition:
  - DP 9 target distance is in decimetres (z2m divideBy10); the built-in
    quirk divides by 100, showing a distance 10x too small.
  - DP 1 is none/presence/move; besides occupancy, expose it as a state sensor.
  - DP 3/4 min/max range: 0-6 m / 0.5-9 m in 0.5 m steps for this
    manufacturer (the built-in quirk uses another model's 0.75 m steps).
  - DP 2/102 sensitivities are 1-10.
  - DP 104 is sent after every DP 1 change (seen: 0 vacant, 1 occupied, 7)
    and is mapped by neither z2m nor zha; exposed raw as a diagnostic sensor.
Attribute names match the built-in quirk so existing entities are kept.
"""

import zigpy.types as t
from zigpy.zcl import foundation
from zigpy.zcl.clusters.measurement import OccupancySensing

from zhaquirks.builder import (
    EntityPlatform,
    EntityType,
    SensorDeviceClass,
    SensorStateClass,
    UnitOfLength,
    UnitOfTime,
)
from zhaquirks.tuya import TUYA_CLUSTER_ID
from zhaquirks.tuya.builder import TuyaQuirkBuilder
from zhaquirks.tuya.mcu import DPToAttributeMapping, TuyaMCUCluster
from zhaquirks.tuya.tuya_motion import TuyaOccupancySensing


class PresenceState(t.enum8):
    """DP 1 presence state."""

    none = 0x00
    presence = 0x01
    move = 0x02


(
    TuyaQuirkBuilder("_TZE204_gkfbdvyx", "TS0601")
    .applies_to("_TZE200_gkfbdvyx", "TS0601")
    .adds(TuyaOccupancySensing)
    .tuya_attribute(
        dp_id=1,
        attribute_name="presence_state",
        type=PresenceState,
        access=foundation.ZCLAttributeAccess.Read
        | foundation.ZCLAttributeAccess.Report,
    )
    .tuya_dp_multi(
        dp_id=1,
        attribute_mapping=[
            DPToAttributeMapping(
                TuyaOccupancySensing.ep_attribute,
                OccupancySensing.AttributeDefs.occupancy.name,
                converter=lambda x: x in (1, 2),
            ),
            DPToAttributeMapping(
                TuyaMCUCluster.ep_attribute,
                "presence_state",
                converter=lambda x: PresenceState(x) if x in (0, 1, 2) else PresenceState.none,
            ),
        ],
    )
    .enum(
        "presence_state",
        PresenceState,
        TUYA_CLUSTER_ID,
        entity_platform=EntityPlatform.SENSOR,
        entity_type=EntityType.STANDARD,
        translation_key="presence_state",
        fallback_name="Presence state",
    )
    .tuya_number(
        dp_id=2,
        attribute_name="move_sensitivity",
        type=t.uint16_t,
        min_value=1,
        max_value=10,
        step=1,
        translation_key="move_sensitivity",
        fallback_name="Motion sensitivity",
    )
    .tuya_number(
        dp_id=3,
        attribute_name="detection_distance_min",
        type=t.uint16_t,
        device_class=SensorDeviceClass.DISTANCE,
        unit=UnitOfLength.METERS,
        min_value=0,
        max_value=6,
        step=0.5,
        multiplier=0.01,
        translation_key="detection_distance_min",
        fallback_name="Minimum range",
    )
    .tuya_number(
        dp_id=4,
        attribute_name="detection_distance_max",
        type=t.uint16_t,
        device_class=SensorDeviceClass.DISTANCE,
        unit=UnitOfLength.METERS,
        min_value=0.5,
        max_value=9.0,
        step=0.5,
        multiplier=0.01,
        translation_key="detection_distance_max",
        fallback_name="Maximum range",
    )
    .tuya_sensor(
        dp_id=9,
        attribute_name="distance",
        type=t.uint16_t,
        divisor=10,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.DISTANCE,
        unit=UnitOfLength.METERS,
        entity_type=EntityType.STANDARD,
        translation_key="distance",
        fallback_name="Target distance",
    )
    .tuya_switch(
        dp_id=101,
        attribute_name="distance_tracking",
        entity_type=EntityType.CONFIG,
        translation_key="distance_tracking",
        fallback_name="Distance tracking",
    )
    .tuya_number(
        dp_id=102,
        attribute_name="presence_sensitivity",
        type=t.uint16_t,
        min_value=1,
        max_value=10,
        step=1,
        translation_key="presence_sensitivity",
        fallback_name="Presence sensitivity",
    )
    .tuya_illuminance(dp_id=103)
    .tuya_sensor(
        dp_id=104,
        attribute_name="dp104_state",
        type=t.uint8_t,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="dp104_state",
        fallback_name="DP 104 (unknown)",
    )
    .tuya_number(
        dp_id=105,
        attribute_name="presence_timeout",
        type=t.uint16_t,
        device_class=SensorDeviceClass.DURATION,
        unit=UnitOfTime.SECONDS,
        min_value=1,
        max_value=15000,
        step=1,
        translation_key="fading_time",
        fallback_name="Fading time",
    )
    .skip_configuration()
    .add_to_registry()
)
