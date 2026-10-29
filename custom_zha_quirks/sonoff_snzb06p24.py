"""SONOFF SNZB-06P24 24 GHz presence sensor with illuminance.

No zha-quirks quirk exists (2026-10); without one ZHA only exposes occupancy,
illuminance and the occupied->unoccupied delay. Following zigbee2mqtt's
"SNZB-06P24" definition, this exposes the private 0xFC11 (eWeLink) settings:
    0x2016 occupancy_zone_enable  bitmap16: 8 zones of 0.5 m (0-4 m); z2m
           merges zones 1+2 (bits 0|1 = 0-1 m), so 7 switches: zone 1 =
           0-1 m, zone n (n >= 2) = bit n = (n*0.5)-(n*0.5+0.5) m
    0x2018 illumination_compensation_offset  int16 lx (-1000..1000)
    0x2021 radar_sensitivity  int8 (-6..+6, fine tune)
    cmd 0x04 spatial learning: send [0x00, seq u64 LE]; the device answers
           with cmd 0x04 sub 0x01 (started) and later sub 0x02 with
           state/reason bytes at payload[9]/[10] (0/0 = success)
Sonoff sends/accepts these without a manufacturer code.
The zone switches and the learning state are virtual (local) attributes.
"""

import time
from typing import Final

from zigpy import types as t
from zigpy.zcl import foundation
from zigpy.zcl.foundation import BaseAttributeDefs, ZCLAttributeDef, ZCLCommandDef

from zhaquirks.builder import EntityPlatform, EntityType, QuirkBuilder
from zhaquirks.clusters import CustomCluster

_ZONES = 7
_ZONE_ENABLE = 0x2016
_VIRT_ZONE_BASE = 0xEF00  # zone n -> 0xEF00 + n
_LEARN_STATE = 0xEF10
_LEARN_CMD = 0x04


class _RawBytes(bytes):
    """Raw command payload (no length prefix)."""

    def serialize(self) -> bytes:
        return bytes(self)

    @classmethod
    def deserialize(cls, data: bytes) -> tuple["_RawBytes", bytes]:
        return cls(data), b""


class SpatialLearningState(t.enum8):
    """Result of the spatial learning calibration."""

    Clear = 0x00
    Learning = 0x01
    Failed = 0x02


def _zone_mask(zone: int) -> int:
    return 0b11 if zone == 1 else 1 << zone


def _zone_attr(zone: int) -> ZCLAttributeDef:
    return ZCLAttributeDef(id=_VIRT_ZONE_BASE + zone, type=t.Bool)


class SonoffSNZB06P24Cluster(CustomCluster):
    """eWeLink private cluster 0xFC11."""

    cluster_id = 0xFC11
    name = "Sonoff SNZB-06P24"
    ep_attribute = "sonoff_snzb06p24"

    class AttributeDefs(BaseAttributeDefs):
        """Attribute definitions."""

        occupancy_zone_enable: Final = ZCLAttributeDef(
            id=_ZONE_ENABLE, type=t.bitmap16, manufacturer_code=None
        )
        illumination_compensation_offset: Final = ZCLAttributeDef(
            id=0x2018, type=t.int16s, manufacturer_code=None
        )
        radar_sensitivity: Final = ZCLAttributeDef(
            id=0x2021, type=t.int8s, manufacturer_code=None
        )
        zone_1: Final = _zone_attr(1)
        zone_2: Final = _zone_attr(2)
        zone_3: Final = _zone_attr(3)
        zone_4: Final = _zone_attr(4)
        zone_5: Final = _zone_attr(5)
        zone_6: Final = _zone_attr(6)
        zone_7: Final = _zone_attr(7)
        spatial_learning_state: Final = ZCLAttributeDef(
            id=_LEARN_STATE, type=SpatialLearningState
        )

    class ServerCommandDefs(foundation.BaseCommandDefs):
        """Server command definitions."""

        spatial_learning: Final = ZCLCommandDef(
            id=_LEARN_CMD,
            schema={"data?": _RawBytes},
            manufacturer_code=None,
        )

    class ClientCommandDefs(foundation.BaseCommandDefs):
        """Client command definitions."""

        spatial_learning_report: Final = ZCLCommandDef(
            id=_LEARN_CMD,
            schema={"data": _RawBytes},
            manufacturer_code=None,
        )

    def _update_attribute(self, attrid, value):
        super()._update_attribute(attrid, value)
        if attrid == _ZONE_ENABLE and value is not None:
            for zone in range(1, _ZONES + 1):
                super()._update_attribute(
                    _VIRT_ZONE_BASE + zone, t.Bool(bool(int(value) & _zone_mask(zone)))
                )

    def _is_virtual_zone(self, attr) -> int | None:
        attr_id = self.find_attribute(attr).id
        zone = attr_id - _VIRT_ZONE_BASE
        return zone if 1 <= zone <= _ZONES else None

    async def write_attributes(self, attributes, manufacturer=None, **kwargs):
        """Fold zone switch writes into one 0x2016 bitmap write."""
        real, zones = {}, {}
        for key, value in attributes.items():
            zone = self._is_virtual_zone(key)
            if zone is not None:
                zones[zone] = bool(value)
            else:
                real[key] = value
        if zones:
            bitmap = int(self.get(_ZONE_ENABLE) or 0xFFFF)
            for zone, enabled in zones.items():
                if enabled:
                    bitmap |= _zone_mask(zone)
                else:
                    bitmap &= ~_zone_mask(zone)
            real[_ZONE_ENABLE] = t.bitmap16(bitmap)
        result = await super().write_attributes(real, manufacturer, **kwargs)
        # a successful write does not refresh the virtual zone switches (the
        # device sends no report back), so ZHA reverted them in the UI
        if zones:
            records = result[0] if result else []
            if not isinstance(records, list):
                records = getattr(records, "status_records", [records])
            if all(
                getattr(rec, "status", rec) == foundation.Status.SUCCESS
                for rec in records
            ):
                self._update_attribute(_ZONE_ENABLE, real[_ZONE_ENABLE])
        return result

    async def read_attributes(self, attributes, *args, **kwargs):
        """Serve virtual attributes locally; zone switches via 0x2016."""
        real, virt = [], []
        for attr in attributes:
            attr_id = self.find_attribute(attr).id
            (virt if attr_id == _LEARN_STATE or self._is_virtual_zone(attr) else real).append(attr)
        if any(self._is_virtual_zone(a) for a in virt) and not any(
            self.find_attribute(a).id == _ZONE_ENABLE for a in real
        ):
            real.append(_ZONE_ENABLE)
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

    async def command(self, command_id, *args, **kwargs):
        """Fill in the spatial learning payload (sub 0x00 + timestamp)."""
        if command_id == _LEARN_CMD and not args and not kwargs.get("data"):
            seq = int(time.time() * 1000).to_bytes(8, "little")
            kwargs["data"] = _RawBytes(b"\x00" + seq)
            self._update_attribute(_LEARN_STATE, SpatialLearningState.Learning)
        return await super().command(command_id, *args, **kwargs)

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        """Track spatial learning responses."""
        if hdr.command_id == _LEARN_CMD:
            data = bytes(getattr(args, "data", b"") or b"")
            self.debug("spatial learning report: %s", data.hex())
            if data[:1] == b"\x01":
                self._update_attribute(_LEARN_STATE, SpatialLearningState.Learning)
            elif data[:1] == b"\x02" and len(data) >= 11:
                ok = data[9] == 0 and data[10] == 0
                self._update_attribute(
                    _LEARN_STATE,
                    SpatialLearningState.Clear if ok else SpatialLearningState.Failed,
                )
            return
        super().handle_cluster_request(hdr, args, dst_addressing=dst_addressing)


_ZONE_RANGES = ["0-1 m"] + [f"{n * 0.5:g}-{n * 0.5 + 0.5:g} m" for n in range(2, _ZONES + 1)]

builder = (
    QuirkBuilder("SONOFF", "SNZB-06P24")
    .replaces(SonoffSNZB06P24Cluster)
    .number(
        SonoffSNZB06P24Cluster.AttributeDefs.radar_sensitivity.name,
        SonoffSNZB06P24Cluster.cluster_id,
        min_value=-6,
        max_value=6,
        step=1,
        mode="slider",
        translation_key="radar_sensitivity",
        fallback_name="Fine-tune sensitivity",
    )
    .number(
        SonoffSNZB06P24Cluster.AttributeDefs.illumination_compensation_offset.name,
        SonoffSNZB06P24Cluster.cluster_id,
        min_value=-1000,
        max_value=1000,
        step=1,
        unit="lx",
        mode="box",
        translation_key="illuminance_calibration",
        fallback_name="Illuminance calibration",
    )
    .command_button(
        SonoffSNZB06P24Cluster.ServerCommandDefs.spatial_learning.name,
        SonoffSNZB06P24Cluster.cluster_id,
        entity_type=EntityType.CONFIG,
        translation_key="spatial_learning",
        fallback_name="Start spatial learning",
    )
    .enum(
        SonoffSNZB06P24Cluster.AttributeDefs.spatial_learning_state.name,
        SpatialLearningState,
        SonoffSNZB06P24Cluster.cluster_id,
        entity_platform=EntityPlatform.SENSOR,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="spatial_learning_state",
        fallback_name="Spatial learning state",
    )
)
for _zone in range(1, _ZONES + 1):
    builder = builder.switch(
        f"zone_{_zone}",
        SonoffSNZB06P24Cluster.cluster_id,
        translation_key=f"occupancy_zone_{_zone}",
        fallback_name=f"Zone {_zone} ({_ZONE_RANGES[_zone - 1]})",
        # virtual: read 0x2016 at startup instead of trusting the cache
        attribute_initialized_from_cache=False,
    )
builder.add_to_registry()
