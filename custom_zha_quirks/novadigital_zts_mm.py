"""Nova Digital ZTS-MM — mmWave presence radar 5.8 GHz (Tuya TS0601).

Copy this file to the root of the ZHA custom quirks folder
(custom_quirks_path, e.g. /config/custom_zha_quirks/) and restart Home
Assistant.

A whitelabel of Tuya's ZY_HPS01 (_TZE204_lbbg34rj). zha-quirks already ships
a quirk for ZY_HPS01 (zhaquirks/tuya/tuya_motion.py), but only for
_TZE204_ex3rcdha, so this device gets no entities without this file.

Datapoints from zigbee-herdsman-converters (src/devices/tuya.ts, ZY_HPS01):
    12  illuminance            raw, lx
    101 occupancy              0 = occupied (trueFalse0)
    104 presence_timeout       s, 0-180
    105 move_sensitivity       0-10
    107 breath_sensitivity     0-10
    109 move_maximum_range     cm, 0-600
    110 move_minimum_range     cm, 0-600
    111 breath_maximum_range   cm, 0-600
    112 breath_minimum_range   cm, 0-600
Note: zha-quirks' ZY_HPS01 quirk maps 111 to the breath MINIMUM and 112 to
the MAXIMUM, the reverse of zigbee-herdsman-converters; this follows the
latter. If the two breath-range numbers turn out swapped on the real
device, swap dp_id 111/112 below.
"""

import zigpy.types as t
from zigpy.zcl.clusters.measurement import OccupancySensing

from zhaquirks.builder import UnitOfLength, UnitOfTime
from zhaquirks.tuya import TuyaLocalCluster
from zhaquirks.tuya.builder import TuyaQuirkBuilder


class NovaDigitalOccupancySensing(OccupancySensing, TuyaLocalCluster):
    """Occupancy fed by Tuya DP 101."""


(
    TuyaQuirkBuilder("_TZE204_lbbg34rj", "TS0601")
    .friendly_name(model="ZTS-MM", manufacturer="Nova Digital")
    .tuya_illuminance(dp_id=12)
    .tuya_dp(
        dp_id=101,
        ep_attribute=NovaDigitalOccupancySensing.ep_attribute,
        attribute_name=OccupancySensing.AttributeDefs.occupancy.name,
        converter=lambda x: x == 0,
    )
    .adds(NovaDigitalOccupancySensing)
    .tuya_number(
        dp_id=104,
        attribute_name="presence_timeout",
        type=t.uint16_t,
        unit=UnitOfTime.SECONDS,
        min_value=0,
        max_value=180,
        step=1,
        translation_key="presence_timeout",
        fallback_name="Presence timeout",
    )
    .tuya_number(
        dp_id=105,
        attribute_name="move_sensitivity",
        type=t.uint16_t,
        min_value=0,
        max_value=10,
        step=1,
        translation_key="move_sensitivity",
        fallback_name="Motion sensitivity",
    )
    .tuya_number(
        dp_id=107,
        attribute_name="breath_sensitivity",
        type=t.uint16_t,
        min_value=0,
        max_value=10,
        step=1,
        translation_key="breath_sensitivity",
        fallback_name="Breath sensitivity",
    )
    .tuya_number(
        dp_id=110,
        attribute_name="move_minimum_range",
        type=t.uint16_t,
        unit=UnitOfLength.CENTIMETERS,
        min_value=0,
        max_value=600,
        step=10,
        translation_key="move_minimum_range",
        fallback_name="Motion minimum range",
    )
    .tuya_number(
        dp_id=109,
        attribute_name="move_maximum_range",
        type=t.uint16_t,
        unit=UnitOfLength.CENTIMETERS,
        min_value=0,
        max_value=600,
        step=10,
        translation_key="move_maximum_range",
        fallback_name="Motion maximum range",
    )
    .tuya_number(
        dp_id=112,
        attribute_name="breath_minimum_range",
        type=t.uint16_t,
        unit=UnitOfLength.CENTIMETERS,
        min_value=0,
        max_value=600,
        step=10,
        translation_key="breath_minimum_range",
        fallback_name="Breath minimum range",
    )
    .tuya_number(
        dp_id=111,
        attribute_name="breath_maximum_range",
        type=t.uint16_t,
        unit=UnitOfLength.CENTIMETERS,
        min_value=0,
        max_value=600,
        step=10,
        translation_key="breath_maximum_range",
        fallback_name="Breath maximum range",
    )
    .skip_configuration()
    .add_to_registry()
)
