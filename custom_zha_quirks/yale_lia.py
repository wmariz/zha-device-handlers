"""Yale LIA digital lock (Zigbee, model "LIA").

Plain ZCL DoorLock (0x0101), no PowerConfiguration cluster, so there is no
battery to read (zigbee2mqtt's generic Yale lockExtend lists one anyway).
Following z2m ("LIA": lockExtend + lia_action), this adds what ZHA lacks:
  - operation_event_notification (0x20) tolerant of the Lia's short frames
    (z2m parses them raw because pin/local_time/data may be missing) and with
    source 4 = fingerprint (z2m: verified on a real device; ZCL says
    "indeterminate"). Besides ZHA's own zha_event, the last action, its
    source and user id are kept as sensors.
  - config entities for standard DoorLock attributes the device answers:
    auto_relock_time (0x0023, s), sound_volume (0x0024), wrong_code_entry_limit
    (0x0030) and user_code_temporary_disable_time (0x0031, s).
Read on 2026-10-22: door_state (0x0003) unsupported, auto relock 30 s,
volume 0, wrong code limit 5, disable time 60 s, firmware 02.03.04.
"""

from typing import Final

from zigpy import types as t
from zigpy.zcl import foundation
from zigpy.zcl.clusters.closures import DoorLock
from zigpy.zcl.foundation import ZCLAttributeDef, ZCLCommandDef

from zhaquirks.builder import EntityPlatform, EntityType, QuirkBuilder
from zhaquirks.clusters import CustomCluster
from zhaquirks.builder import SensorDeviceClass, UnitOfTime

_LAST_ACTION = 0xEF00
_LAST_SOURCE = 0xEF01
_LAST_USER = 0xEF02
_VIRTUAL = (_LAST_ACTION, _LAST_SOURCE, _LAST_USER)


class LiaEventSource(t.enum8):
    """Operation event source (4 = fingerprint on the Lia)."""

    Keypad = 0x00
    RF = 0x01
    Manual = 0x02
    RFID = 0x03
    Fingerprint = 0x04
    Indeterminate = 0xFF


class LiaSoundVolume(t.enum8):
    """Sound volume (ZCL DoorLock 0x0024)."""

    Silent = 0x00
    Low = 0x01
    High = 0x02


class YaleLiaDoorLock(CustomCluster, DoorLock):
    """DoorLock with tolerant operation events and last-action attributes."""

    class AttributeDefs(DoorLock.AttributeDefs):
        """Attribute definitions."""

        sound_volume: Final = ZCLAttributeDef(
            id=0x0024, type=LiaSoundVolume, access="r*wp"
        )
        last_action: Final = ZCLAttributeDef(
            id=_LAST_ACTION, type=DoorLock.OperationEvent
        )
        last_action_source: Final = ZCLAttributeDef(
            id=_LAST_SOURCE, type=LiaEventSource
        )
        last_action_user: Final = ZCLAttributeDef(id=_LAST_USER, type=t.uint16_t)

    class ClientCommandDefs(DoorLock.ClientCommandDefs):
        """Client command definitions."""

        operation_event_notification: Final = ZCLCommandDef(
            id=0x20,
            schema={
                "operation_event_source": LiaEventSource,
                "operation_event_code": DoorLock.OperationEvent,
                "user_id": t.uint16_t,
                "pin?": t.CharacterString,
                "local_time?": t.LocalTime,
                "data?": t.CharacterString,
            },
        )

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        """Keep the last operation event as local attributes."""
        if hdr.command_id == 0x20 and not hdr.frame_control.is_general:
            self._update_attribute(_LAST_ACTION, args.operation_event_code)
            self._update_attribute(_LAST_SOURCE, args.operation_event_source)
            self._update_attribute(_LAST_USER, args.user_id)
        super().handle_cluster_request(hdr, args, dst_addressing=dst_addressing)

    async def read_attributes(self, attributes, *args, **kwargs):
        """Serve the last-action attributes from the local cache."""
        real, virt = [], []
        for attr in attributes:
            (virt if self.find_attribute(attr).id in _VIRTUAL else real).append(attr)
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
    QuirkBuilder("Yale", "LIA")
    .replaces(YaleLiaDoorLock)
    .enum(
        "last_action",
        DoorLock.OperationEvent,
        DoorLock.cluster_id,
        entity_platform=EntityPlatform.SENSOR,
        entity_type=EntityType.STANDARD,
        translation_key="last_action",
        fallback_name="Last action",
    )
    .enum(
        "last_action_source",
        LiaEventSource,
        DoorLock.cluster_id,
        entity_platform=EntityPlatform.SENSOR,
        entity_type=EntityType.STANDARD,
        translation_key="last_action_source",
        fallback_name="Last action source",
    )
    .sensor(
        "last_action_user",
        DoorLock.cluster_id,
        entity_type=EntityType.STANDARD,
        translation_key="last_action_user",
        fallback_name="Last action user",
    )
    .number(
        "auto_relock_time",
        DoorLock.cluster_id,
        min_value=0,
        max_value=3600,
        step=1,
        unit=UnitOfTime.SECONDS,
        mode="box",
        translation_key="auto_relock_time",
        fallback_name="Auto relock time",
    )
    .enum(
        "sound_volume",
        LiaSoundVolume,
        DoorLock.cluster_id,
        translation_key="sound_volume",
        fallback_name="Sound volume",
    )
    .number(
        "wrong_code_entry_limit",
        DoorLock.cluster_id,
        min_value=1,
        max_value=255,
        step=1,
        mode="box",
        translation_key="wrong_code_entry_limit",
        fallback_name="Wrong code entry limit",
    )
    .number(
        "user_code_temporary_disable_time",
        DoorLock.cluster_id,
        min_value=1,
        max_value=255,
        step=1,
        unit=UnitOfTime.SECONDS,
        mode="box",
        translation_key="user_code_temporary_disable_time",
        fallback_name="Wrong code lockout time",
    )
    .add_to_registry()
)
