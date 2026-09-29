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
the MAXIMUM, the reverse of zigbee-herdsman-converters. Verified on real
hardware (2026-09-29) that z2m is right: with DP 112=0 / DP 111=600 a still
person kept presence on; with DP 112=600 / DP 111=0 (empty breath window)
presence dropped after the fade time while the person stayed still.

DEFAULT_SETTINGS (below) are written to each device once, on the first
message received from it; see NovaDigitalMCUCluster.
"""

import asyncio
import json
import logging
import os

import zigpy.types as t
from zigpy.zcl.clusters.measurement import OccupancySensing

from zhaquirks.builder import UnitOfLength, UnitOfTime
from zhaquirks.tuya import TUYA_QUERY_DATA, TuyaLocalCluster
from zhaquirks.tuya.builder import TuyaQuirkBuilder
from zhaquirks.tuya.mcu import TuyaMCUCluster

_LOGGER = logging.getLogger(__name__)

# Settings written to the device ONCE per device (the first message received
# from it after this quirk version loads), then never again, so later
# changes made in Home Assistant stick.
DEFAULT_SETTINGS = {
    "presence_timeout": 10,  # s
    "move_sensitivity": 5,
    "breath_sensitivity": 5,
    "move_minimum_range": 100,  # cm
    "move_maximum_range": 600,
    "breath_minimum_range": 100,
    "breath_maximum_range": 600,
}
# Devices (IEEE) that already got DEFAULT_SETTINGS. Delete the device's entry
# (or the whole file) and restart Home Assistant to apply them again.
_DEFAULTS_STORE = "/config/.storage/novadigital_zts_mm_defaults.json"


def _load_applied() -> set[str]:
    try:
        with open(_DEFAULTS_STORE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return set()


def _save_applied(applied: set[str]) -> None:
    os.makedirs(os.path.dirname(_DEFAULTS_STORE), exist_ok=True)
    with open(_DEFAULTS_STORE, "w") as f:
        json.dump(sorted(applied), f)


_APPLIED = _load_applied()


class NovaDigitalOccupancySensing(OccupancySensing, TuyaLocalCluster):
    """Occupancy fed by Tuya DP 101."""


class NovaDigitalMCUCluster(TuyaMCUCluster):
    """Tuya MCU cluster that queries all datapoints once per HA session and
    writes DEFAULT_SETTINGS once per device."""

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        super().handle_cluster_request(hdr, args, dst_addressing=dst_addressing)
        device = self.endpoint.device
        loop = asyncio.get_running_loop()
        # The device reports only presence and illuminance on its own, so the
        # settings numbers stay "unknown" (e.g. after re-pairing) until asked.
        if not getattr(device, "_zts_mm_queried", False):
            device._zts_mm_queried = True
            loop.create_task(self._query_all_datapoints())
        ieee = str(device.ieee)
        if ieee in _APPLIED or getattr(device, "_zts_mm_defaults_pending", False):
            return
        device._zts_mm_defaults_pending = True
        loop.create_task(self._apply_defaults(ieee))

    async def _query_all_datapoints(self) -> None:
        """Tuya data query: the device answers with every datapoint's value."""
        try:
            await self.command(TUYA_QUERY_DATA, expect_reply=False)
        except Exception:
            self.endpoint.device._zts_mm_queried = False  # retry on next message
            _LOGGER.warning("ZTS-MM %s: data query failed", self.endpoint.device.ieee, exc_info=True)

    async def _apply_defaults(self, ieee: str) -> None:
        device = self.endpoint.device
        try:
            _LOGGER.info("ZTS-MM %s: writing default settings %s", ieee, DEFAULT_SETTINGS)
            await self.write_attributes(dict(DEFAULT_SETTINGS))
            _APPLIED.add(ieee)
            await asyncio.get_running_loop().run_in_executor(
                None, _save_applied, set(_APPLIED)
            )
        except Exception:
            # Leave it unmarked so the next message from the device retries.
            _LOGGER.warning("ZTS-MM %s: writing default settings failed", ieee, exc_info=True)
        finally:
            device._zts_mm_defaults_pending = False


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
    .add_to_registry(replacement_cluster=NovaDigitalMCUCluster)
)
