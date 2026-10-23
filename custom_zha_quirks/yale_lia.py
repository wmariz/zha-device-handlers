"""Yale LIA digital lock (Zigbee, model "LIA").

Plain ZCL DoorLock (0x0101). PowerConfiguration (0x0001) is NOT in the simple
descriptor, but the lock answers reads of battery_percentage_remaining (0x0021,
0.5 % units; tested 2026-10-23: 198 = 99 %). It rejects Configure Reporting for
it (malformed UNSUPPORTED_ATTRIBUTE reply), so the cluster below re-reads the
battery every 12 h. battery_voltage (0x0020) is unsupported.
Following z2m ("LIA": lockExtend + lia_action), this adds what ZHA lacks:
  - operation_event_notification (0x20) tolerant of the Lia's short frames
    (z2m parses them raw because pin/local_time/data may be missing) and with
    source 4 = fingerprint (z2m: verified on a real device; ZCL says
    "indeterminate"). Besides ZHA's own zha_event, the last action, its
    source and user id are kept as sensors.
  - config entities for auto_relock_time (0x0023) and sound_volume (0x0024).
    wrong_code_entry_limit (0x0030 = 5) and user_code_temporary_disable_time
    (0x0031 = 60 s) are readable but fixed per the Lia manual: not exposed.
Read on 2026-10-22: door_state (0x0003) unsupported, auto relock 30 s,
volume 0, wrong code limit 5, disable time 60 s, firmware 02.03.04.
auto_relock_time (verified 2026-10-22): 0 disables the timed auto lock and
N > 0 relocks N s after an unlock (AutoLock event, source Manual) -- but only
while the lock's own door-sensor mode is OFF in its menu; with the door sensor
on, the lock ignores the timer. Setting the time in the lock's menu does not
update 0x0023, so HA may show a stale value until it is written from HA.
"""

import asyncio
import enum
from typing import Final

from zigpy import types as t
from zigpy.types.basic import _EnumMixin, _IntEnumMeta
from zigpy.zcl import ClusterType, foundation
from zigpy.zcl.clusters.closures import DoorLock
from zigpy.zcl.clusters.general import PowerConfiguration, Time
from zigpy.zcl.foundation import DataTypeId, ZCLAttributeDef, ZCLCommandDef

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
    """Sound volume (ZCL DoorLock 0x0024: 0 silent, 1 low, 2 high)."""

    Silencioso = 0x00
    Baixo = 0x01
    Alto = 0x02


class LiaActionSource(t.enum8):
    """Readable operation event source, shown by the last-source sensor."""

    Teclado = 0x00
    locals()["Remoto (Zigbee)"] = 0x01
    Manual = 0x02
    locals()["Cartão"] = 0x03
    Digital = 0x04
    Indeterminado = 0xFF


class LiaAction(t.enum8):
    """Readable ZCL operation event code, shown by the last-action sensor."""

    Desconhecido = 0x00
    Trancou = 0x01
    Destrancou = 0x02
    locals()["Falha ao trancar (código inválido)"] = 0x03
    locals()["Falha ao trancar (fora do horário)"] = 0x04
    locals()["Falha ao destrancar (código inválido)"] = 0x05
    locals()["Falha ao destrancar (fora do horário)"] = 0x06
    locals()["Trancou pelo painel"] = 0x07
    locals()["Trancou com a chave"] = 0x08
    locals()["Destrancou com a chave"] = 0x09
    locals()["Trancamento automático"] = 0x0A
    locals()["Trancou por agenda"] = 0x0B
    locals()["Destrancou por agenda"] = 0x0C
    locals()["Trancou manualmente"] = 0x0D
    locals()["Destrancou manualmente"] = 0x0E
    locals()["Usuário sem acesso"] = 0x0F


class LiaAutoRelock(_EnumMixin, t.uint32_t, enum.Enum, metaclass=_IntEnumMeta):
    """Auto relock time choices (0x0023 is uint32 seconds; 0 = off).

    The lock accepts any value from HA (5 s verified), not just the menu's
    15/30/45 s.
    """

    Desativado = 0
    # member names are what the HA select shows
    locals()["5 s"] = 5
    locals()["10 s"] = 10
    locals()["15 s"] = 15
    locals()["30 s"] = 30
    locals()["45 s"] = 45
    locals()["60 s"] = 60


_BATTERY_POLL_S = 12 * 3600
_POLL_FLAG = "_yale_lia_battery_poll"  # on the device: module may load twice


class YaleLiaPowerConfiguration(CustomCluster, PowerConfiguration):
    """Battery cluster the lock answers but does not report: poll it."""

    def __init__(self, *args, **kwargs):
        """Start one battery poll loop per device."""
        super().__init__(*args, **kwargs)
        device = self.endpoint.device
        task = getattr(device, _POLL_FLAG, None)
        if task is None or task.done():
            try:
                setattr(device, _POLL_FLAG, asyncio.get_running_loop().create_task(
                    self._poll_battery()
                ))
            except RuntimeError:  # no running loop (e.g. import-time tests)
                pass

    async def _poll_battery(self) -> None:
        await asyncio.sleep(120)
        while True:
            try:
                await self.read_attributes(
                    ["battery_percentage_remaining"], allow_cache=False
                )
            except Exception as exc:  # noqa: BLE001 - lock asleep/unreachable
                self.debug("battery poll failed: %r", exc)
            await asyncio.sleep(_BATTERY_POLL_S)


class YaleLiaDoorLock(CustomCluster, DoorLock):
    """DoorLock with tolerant operation events and last-action attributes."""

    class AttributeDefs(DoorLock.AttributeDefs):
        """Attribute definitions."""

        auto_relock_time: Final = ZCLAttributeDef(
            id=0x0023, type=LiaAutoRelock, zcl_type=DataTypeId.uint32, access="r*wp"
        )
        # the lock rejects enum8 writes (INVALID_DATA_TYPE): send as uint8
        sound_volume: Final = ZCLAttributeDef(
            id=0x0024, type=LiaSoundVolume, zcl_type=DataTypeId.uint8, access="r*wp"
        )
        last_action: Final = ZCLAttributeDef(id=_LAST_ACTION, type=LiaAction)
        last_action_source: Final = ZCLAttributeDef(
            id=_LAST_SOURCE, type=LiaActionSource
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
            # readable names for the sensors; zha_event keeps the ZCL names
            self._update_attribute(_LAST_ACTION, LiaAction(int(args.operation_event_code)))
            self._update_attribute(
                _LAST_SOURCE, LiaActionSource(int(args.operation_event_source))
            )
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
    # The lock polls the coordinator's Time cluster every few seconds (Read
    # Attributes time + time_status on cluster 0x000A) but does not declare a
    # Time client, so zigpy dropped the request ("Ignoring message on unknown
    # cluster: 0x000a"). With a Time client on EP1, zigpy's Time cluster
    # answers it (handle_read_attribute_time / _time_status).
    .adds(Time, cluster_type=ClusterType.Client)
    # not in the simple descriptor, but battery reads are answered (see top)
    .adds(YaleLiaPowerConfiguration)
    .enum(
        "last_action",
        LiaAction,
        DoorLock.cluster_id,
        entity_platform=EntityPlatform.SENSOR,
        entity_type=EntityType.STANDARD,
        translation_key="last_action",
        fallback_name="Last action",
    )
    .enum(
        "last_action_source",
        LiaActionSource,
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
    .enum(
        "auto_relock_time",
        LiaAutoRelock,
        DoorLock.cluster_id,
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
    .add_to_registry()
)
