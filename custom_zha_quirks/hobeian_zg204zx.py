"""HOBEIAN ZG-204ZX 24 GHz presence + temperature/humidity sensor (battery).

Without a quirk ZHA already shows presence, temperature, humidity,
illuminance and battery: the device sends each value twice, on the standard
clusters (0x0406/0x0500/0x0402/0x0405/0x0400/0x0001) and as Tuya DPs on 0xEF00
(verified 2026-10-29: DP 106 = 470 lx matched 0x0400 = 26716, DP 111 = 228,
DP 101 = 71). Following zigbee2mqtt's "ZG-204ZX" definition, the settings
only reachable over 0xEF00 are exposed here:
    DP 2   static detection sensitivity   0-10
    DP 4   detection distance             0-5 m (raw cm)
    DP 102 fading (presence keep) time    0-28800 s
    DP 103 anti-interference              on/off
    DP 104 humidity calibration           -30..30 %
    DP 105 temperature calibration        -2..2 C (raw /10)
    DP 107 illuminance sampling interval  1-720 min
    DP 108 LED indicator                  on/off
    DP 123 motion detection sensitivity   0-10
DPs 1, 101, 106, 110 and 111 duplicate the standard clusters and are mapped
to hidden attributes only (DP 109 temperature unit is left unmapped).
The IAS zone binary sensor duplicates the occupancy one, and the 0x0406
PIR delays have no effect (the timeout is DP 102), so those entities are
not created.
"""

from zigpy.quirks.v2.homeassistant import UnitOfLength, UnitOfTime
import zigpy.types as t
from zigpy.zcl.clusters.measurement import OccupancySensing
from zigpy.zcl.clusters.security import IasZone

from zhaquirks.tuya.builder import TuyaQuirkBuilder

(
    TuyaQuirkBuilder("HOBEIAN", "ZG-204ZX")
    # duplicates of the standard clusters: keep them out of the log
    .tuya_dp_attribute(dp_id=1, attribute_name="presence_dp", type=t.uint8_t)
    .tuya_dp_attribute(dp_id=101, attribute_name="humidity_dp", type=t.uint32_t)
    .tuya_dp_attribute(dp_id=106, attribute_name="illuminance_dp", type=t.uint32_t)
    .tuya_dp_attribute(dp_id=110, attribute_name="battery_dp", type=t.uint32_t)
    .tuya_dp_attribute(dp_id=111, attribute_name="temperature_dp", type=t.int32s)
    .tuya_number(
        dp_id=102,
        attribute_name="fading_time",
        type=t.uint32_t,
        min_value=0,
        max_value=28800,
        step=1,
        unit=UnitOfTime.SECONDS,
        mode="box",
        translation_key="fading_time",
        fallback_name="Fading time",
    )
    .tuya_number(
        dp_id=4,
        attribute_name="detection_distance",
        type=t.uint32_t,
        min_value=0,
        max_value=5,
        step=0.1,
        multiplier=0.01,
        unit=UnitOfLength.METERS,
        translation_key="detection_distance",
        fallback_name="Detection distance",
    )
    .tuya_number(
        dp_id=123,
        attribute_name="motion_detection_sensitivity",
        type=t.uint32_t,
        min_value=0,
        max_value=10,
        step=1,
        translation_key="motion_detection_sensitivity",
        fallback_name="Motion detection sensitivity",
    )
    .tuya_number(
        dp_id=2,
        attribute_name="static_detection_sensitivity",
        type=t.uint32_t,
        min_value=0,
        max_value=10,
        step=1,
        translation_key="static_detection_sensitivity",
        fallback_name="Static detection sensitivity",
    )
    .tuya_switch(
        dp_id=108,
        attribute_name="indicator",
        translation_key="led_indicator",
        fallback_name="LED indicator",
    )
    .tuya_switch(
        dp_id=103,
        attribute_name="anti_interference",
        translation_key="anti_interference",
        fallback_name="Anti-interference",
    )
    .tuya_number(
        dp_id=107,
        attribute_name="illuminance_interval",
        type=t.uint32_t,
        min_value=1,
        max_value=720,
        step=1,
        unit=UnitOfTime.MINUTES,
        mode="box",
        translation_key="illuminance_interval",
        fallback_name="Illuminance sampling interval",
    )
    .tuya_number(
        dp_id=105,
        attribute_name="temperature_calibration",
        type=t.int32s,
        min_value=-2,
        max_value=2,
        step=0.1,
        multiplier=0.1,
        unit="°C",
        mode="box",
        translation_key="temperature_calibration",
        fallback_name="Temperature calibration",
    )
    .tuya_number(
        dp_id=104,
        attribute_name="humidity_calibration",
        type=t.int32s,
        min_value=-30,
        max_value=30,
        step=1,
        unit="%",
        mode="box",
        translation_key="humidity_calibration",
        fallback_name="Humidity calibration",
    )
    .prevent_default_entity_creation(endpoint_id=1, cluster_id=IasZone.cluster_id)
    .prevent_default_entity_creation(
        endpoint_id=1,
        cluster_id=OccupancySensing.cluster_id,
        unique_id_suffix="pir_o_to_u_delay",
    )
    .prevent_default_entity_creation(
        endpoint_id=1,
        cluster_id=OccupancySensing.cluster_id,
        unique_id_suffix="pir_u_to_o_delay",
    )
    .skip_configuration()
    .add_to_registry()
)
