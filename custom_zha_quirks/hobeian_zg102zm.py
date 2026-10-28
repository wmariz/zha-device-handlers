"""HOBEIAN ZG-102ZM vibration + door/window contact sensor (Tuya MCU).

The device reports vibration both as a standard IAS zone (already exposed
by ZHA) and as Tuya DP 1. Following zigbee2mqtt's "ZG-102ZM" definition it
also has, on the Tuya cluster 0xEF00:
    DP 1   vibration (enum 0/1)       -> kept as a hidden attribute only
    DP 4   battery %                  -> standard PowerConfiguration is used
    DP 6   sensitivity 1-50 (larger = more sensitive; applied only while the
           sensor is awake, i.e. right after a vibration)
    DP 101 contact (magnet) per z2m -- NOT exposed: this unit never sent
           DP 101 (only DPs 1, 4, 6), magnet tests produced only vibration
           (2026-10-28), so it has no reed switch / contact firmware.
"""

import zigpy.types as t

from zhaquirks.tuya.builder import TuyaQuirkBuilder

(
    TuyaQuirkBuilder("HOBEIAN", "ZG-102ZM")
    # DP 1 duplicates the IAS zone vibration; map it so it is not logged
    # as an unhandled datapoint
    .tuya_dp_attribute(dp_id=1, attribute_name="vibration_dp", type=t.uint8_t)
    .tuya_number(
        dp_id=6,
        attribute_name="sensitivity",
        type=t.uint8_t,
        min_value=1,
        max_value=50,
        step=1,
        translation_key="sensitivity",
        fallback_name="Sensitivity",
    )
    .add_to_registry()
)
