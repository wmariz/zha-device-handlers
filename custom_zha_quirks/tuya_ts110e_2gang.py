"""Tuya TS110E 2-channel Zigbee dimmer module (_TZ3210_3mpwqzuu).

Plain ZCL device on endpoints 1 and 2. Without a quirk ZHA only exposes the
two lights; standard move_to_level_with_on_off works (verified 2026-10-29,
level 150 -> reported 0x0000 = 150, 0xF000 = 589). The firmware does not
rescale levels into its min/max range, so the quirk maps HA 1-254 onto
min..max (and back for reports). zigbee2mqtt ("TS110E_2gang_2") also exposes, per channel:
    0x0008 / 0xFC02 switch type        uint8 (0 momentary, 1 toggle, 2 state)
    0x0008 / 0xFC03 min brightness     uint16 0-1000
    0x0008 / 0xFC04 max brightness     uint16 0-1000
    0x0006 / 0x8002 power-on behavior  uint8 (0 off, 1 on, 2 previous)
All four were reported by both endpoints after pairing (FC02 = 0,
FC03 = 11, FC04 = 1000, 8002 = 2). Min/max are exposed as virtual
attributes in % (1-100).
"""

import asyncio
from typing import Final

from zigpy import types as t
from zigpy.quirks import CustomCluster
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import LevelControl, OnOff
from zigpy.zcl.foundation import ZCLAttributeDef

from zhaquirks.builder import QuirkBuilder
from zhaquirks.tuya import PowerOnState


class SwitchType(t.enum8):
    """External (wall) switch type."""

    Momentary = 0x00
    Toggle = 0x01
    State = 0x02


_MIN_RAW = 0xFC03
_MAX_RAW = 0xFC04
_VIRT_MIN = 0xEF10
_VIRT_MAX = 0xEF11
# virtual (%) attribute -> real (0-1000) attribute
_VIRTUAL = {_VIRT_MIN: _MIN_RAW, _VIRT_MAX: _MAX_RAW}
_REAL = {v: k for k, v in _VIRTUAL.items()}
# move_to_level (0x00) and move_to_level_with_on_off (0x04)
_LEVEL_CMDS = {
    LevelControl.ServerCommandDefs.move_to_level.id,
    LevelControl.ServerCommandDefs.move_to_level_with_on_off.id,
}
_MAX_LEVEL = 253
_MOVE_TO_LEVEL = LevelControl.ServerCommandDefs.move_to_level.id
_WITH_ON_OFF = LevelControl.ServerCommandDefs.move_to_level_with_on_off.id
# Last values successfully written by the user (raw 0-1000), kept as virtual
# attributes so zigpy persists them across HA restarts. The firmware does NOT
# keep FC03/FC04 across a power loss: after the breaker is cycled both
# channels come back as 515/767 (seen 2026-11-05) and report them on their
# own; the quirk then writes the wanted values back.
_WANT_MIN = 0xEF20
_WANT_MAX = 0xEF21
_WANT = {_MIN_RAW: _WANT_MIN, _MAX_RAW: _WANT_MAX}
_RESTORE_COOLDOWN = 30  # s, avoids loops if the device keeps refusing
_RESTORE_STEP = 2  # s between the max and the min write
# After power-up the device reports its STORED values, but a few minutes
# later it silently re-calibrates a channel with a non-dimmable lamp to
# 767/767 (no report; seen 2026-11-05 on the Box). Re-check live after these
# delays (s, cumulative 30 s / 2 min / 5 min) and restore if needed.
_RECHECK_DELAYS = (30, 90, 180)


def _raw_to_pct(raw: int) -> int:
    return max(1, min(100, round(raw / 10)))


def _pct_to_raw(pct: int) -> int:
    return max(0, min(1000, int(pct) * 10))


class TS110ELevelControl(CustomCluster, LevelControl):
    """LevelControl with the Tuya switch type and min/max brightness."""

    class AttributeDefs(LevelControl.AttributeDefs):
        """Attribute definitions."""

        tuya_level: Final = ZCLAttributeDef(id=0xF000, type=t.uint16_t)
        switch_type: Final = ZCLAttributeDef(id=0xFC02, type=SwitchType)
        min_brightness_raw: Final = ZCLAttributeDef(id=_MIN_RAW, type=t.uint16_t)
        max_brightness_raw: Final = ZCLAttributeDef(id=_MAX_RAW, type=t.uint16_t)
        min_brightness: Final = ZCLAttributeDef(id=_VIRT_MIN, type=t.uint8_t)
        max_brightness: Final = ZCLAttributeDef(id=_VIRT_MAX, type=t.uint8_t)
        wanted_min_raw: Final = ZCLAttributeDef(id=_WANT_MIN, type=t.uint16_t)
        wanted_max_raw: Final = ZCLAttributeDef(id=_WANT_MAX, type=t.uint16_t)

    _restore_task = None
    _recheck_task = None
    _last_restore = 0.0
    _last_ha_level = None

    def handle_cluster_general_request(self, hdr, args, *, dst_addressing=None):
        """Restore the wanted min/max when the device reports other values.

        After a power loss the dimmer reports FC03/FC04 on its own with its
        default values; that report is the cue to write the wanted ones back.
        """
        super().handle_cluster_general_request(hdr, args, dst_addressing=dst_addressing)
        if hdr.command_id != foundation.GeneralCommand.Report_Attributes:
            return
        reported = {
            a.attrid: a.value.value
            for a in getattr(args, "attribute_reports", [])
            if a.attrid in _WANT
        }
        if reported:
            self._schedule_restore(reported)
            self._schedule_recheck()

    def _schedule_recheck(self) -> None:
        """Re-check min/max a few times after a power-up report."""
        if not any(self.get(w) is not None for w in _WANT.values()):
            return
        if self._recheck_task is not None and not self._recheck_task.done():
            return
        self._recheck_task = asyncio.create_task(self._recheck())

    async def _recheck(self) -> None:
        """Read min/max live after each delay and restore if they drifted."""
        for delay in _RECHECK_DELAYS:
            await asyncio.sleep(delay)
            await self._read_live(list(_WANT))
            drift = self._differs_from_wanted({a: self.get(a) for a in _WANT})
            if drift:
                self.debug("min/max drifted after power-up: %s", drift)
                await self._restore()

    def _differs_from_wanted(self, values: dict[int, int]) -> dict[int, int]:
        """Return {real attr: wanted raw} for values that differ from wanted."""
        out = {}
        for attr_id, value in values.items():
            wanted = self.get(_WANT[attr_id])
            if wanted is not None and value is not None and int(value) != int(wanted):
                out[attr_id] = int(wanted)
        return out

    def _schedule_restore(self, values: dict[int, int]) -> None:
        """Write the wanted values back if the device reported others."""
        if not self._differs_from_wanted(values):
            return
        now = asyncio.get_running_loop().time()
        if now - self._last_restore < _RESTORE_COOLDOWN or (
            self._restore_task is not None and not self._restore_task.done()
        ):
            return
        self._last_restore = now
        self._restore_task = asyncio.create_task(self._restore())

    async def _restore(self) -> None:
        """Write every wanted min/max value to the device."""
        wanted = {
            attr_id: int(self.get(want_id))
            for attr_id, want_id in _WANT.items()
            if self.get(want_id) is not None
        }
        if not wanted:
            return
        self.debug("restoring min/max after the device lost them: %s", wanted)
        # One attribute per frame, max first, with a pause: in a single frame
        # the freshly powered device applies min before max, clamps min to
        # the old max and drops the max (seen 2026-11-05: wanted 1000/1000
        # came back as 767/767).
        for attr_id in sorted(wanted, key=lambda a: a != _MAX_RAW):
            try:
                await self.write_attributes({attr_id: wanted[attr_id]})
            except Exception as exc:  # noqa: BLE001 - radio/timeout
                self.debug("restore of 0x%04X failed: %r", attr_id, exc)
            await asyncio.sleep(_RESTORE_STEP)

    async def command(self, command_id, *args, **kwargs):
        """Cap move-to-level commands at level 253.

        With level 254 (100 %) this firmware accepts the command but does not
        switch the output on (seen 2026-11-05 with min 90 %/max 100 %: the
        Interruptor Banheiro automation at 100 % failed, 99 % and a plain
        turn_on worked). 253 is 99.6 %, indistinguishable in practice.
        """
        if int(command_id) in _LEVEL_CMDS:
            if args and args[0] is not None:
                if int(args[0]) > 0:
                    self._last_ha_level = int(args[0])
                args = (t.uint8_t(self._to_device(int(args[0]))), *args[1:])
            elif kwargs.get("level") is not None:
                if int(kwargs["level"]) > 0:
                    self._last_ha_level = int(kwargs["level"])
                kwargs["level"] = t.uint8_t(self._to_device(int(kwargs["level"])))
        if int(command_id) == _WITH_ON_OFF:
            level = args[0] if args else kwargs.get("level")
            if level is not None and int(level) > 0:
                # The firmware often applies the level of a
                # move_to_level_with_on_off but leaves the output OFF (it
                # answers SUCCESS and reports the level, never on_off=true;
                # seen 2026-11-06 00:02 with level 253 on both channels).
                # A plain OnOff.on() always works, so send the level without
                # the on/off part and then switch on.
                result = await super().command(_MOVE_TO_LEVEL, *args, **kwargs)
                await self.endpoint.on_off.on()
                return result
            if level is not None and int(level) == 0:
                # Turning off with move_to_level_with_on_off(0) (what ZHA
                # sends for light.turn_off with a transition) darkens the
                # output but leaves the OnOff attribute TRUE. ZHA's periodic
                # light refresh (every 45-75 min) then reads on_off = true and
                # shows the light ON while it is dark (seen night of
                # 2026-11-07: ghost "on" at 00:58/01:12/03:15/03:39, lux 0).
                # A plain OnOff.off() switches off and clears the attribute.
                return await self.endpoint.on_off.off()
        return await super().command(command_id, *args, **kwargs)

    def _range(self) -> tuple[int, int] | None:
        """Return the (min, max) output range in raw 0-1000, if known."""
        lo = self.get(_MIN_RAW)
        if lo is None:
            lo = self.get(_WANT_MIN)
        hi = self.get(_MAX_RAW)
        if hi is None:
            hi = self.get(_WANT_MAX)
        if lo is None or hi is None:
            return None
        lo, hi = int(lo), int(hi)
        if hi <= lo or (lo <= 0 and hi >= 1000):
            return None
        return lo, hi

    def _to_device(self, level: int) -> int:
        """Map an HA level (1-254) onto the device's min..max output.

        The firmware does NOT rescale levels into its min/max range: with
        min 20 % (0xFC03 = 200) an HA brightness of 1 % stays dark (seen
        2026-11-06 on the Box PAR20). So 1 -> min and 254 -> max here, and
        the result is capped at 253 (see command()).
        """
        if level <= 0:
            return 0
        rng = self._range()
        if rng is not None:
            lo, hi = rng
            raw = lo + (hi - lo) * (min(level, 254) - 1) / 253
            level = round(raw * 254 / 1000)
        return max(1, min(_MAX_LEVEL, level))

    def _from_device(self, level: int) -> int:
        """Inverse of _to_device, for levels reported by the device."""
        if level <= 0:
            return level
        rng = self._range()
        if rng is None:
            return min(level, 254)
        lo, hi = rng
        raw = level * 1000 / 254
        return max(1, min(254, round(1 + (raw - lo) * 253 / (hi - lo))))

    def _update_attribute(self, attrid, value):
        if attrid == LevelControl.AttributeDefs.current_level.id and value is not None:
            if int(value) == 0xFF:
                # The firmware reports current_level 255 (invalid) after every
                # level change (seen 2026-11-06), which HA shows as 100 %:
                # keep the last level HA asked for instead.
                if self._last_ha_level is None:
                    return
                value = self._last_ha_level
            else:
                value = self._from_device(int(value))
        super()._update_attribute(attrid, value)
        if attrid in _REAL and value is not None:
            super()._update_attribute(_REAL[attrid], _raw_to_pct(value))

    async def write_attributes(self, attributes, manufacturer=None, **kwargs):
        """Write the virtual % attributes to 0xFC03/0xFC04.

        zigpy only updates the attribute cache for records the device
        accepted, which also refreshes the virtual % value through
        _update_attribute. The cache must NOT be updated unconditionally: if
        the device rejected the write, HA would keep showing a value the
        dimmer never stored (seen 2026-11-04: HA showed min 100 % while the
        device kept 51.5 %). After the write, the real attribute is read back
        so HA always shows what the dimmer actually has.
        """
        real = {}
        for key, value in attributes.items():
            attr_id = self.find_attribute(key).id
            if attr_id in _VIRTUAL:
                real[_VIRTUAL[attr_id]] = _pct_to_raw(value)
            else:
                real[key] = value
        result = await super().write_attributes(real, manufacturer, **kwargs)
        written = [a for a in real if isinstance(a, int) and a in _REAL]
        if written:
            records = result[0] if result and isinstance(result[0], list) else []
            failed = {
                r.attrid
                for r in records
                if r.status != foundation.Status.SUCCESS and r.attrid is not None
            }
            if any(r.status != foundation.Status.SUCCESS and r.attrid is None for r in records):
                failed = set(written)  # a global failure: nothing was stored
            for attr_id in written:
                if attr_id not in failed:
                    # remember what the user wants, to restore after a power loss
                    self._update_attribute(_WANT[attr_id], int(real[attr_id]))
            await self._read_live(written)
        return result

    async def _read_live(self, attr_ids):
        """Read real attributes from the device, ignoring the cache.

        Errors (device asleep or unreachable) are swallowed: the caller then
        falls back to whatever is in the cache.
        """
        try:
            await super().read_attributes(attr_ids, allow_cache=False, only_cache=False)
        except Exception as exc:  # noqa: BLE001 - any radio/timeout error
            self.debug("live read of %s failed: %r", attr_ids, exc)

    async def read_attributes(self, attributes, *args, **kwargs):
        """Serve the virtual % attributes by reading 0xFC03/0xFC04.

        The real attributes behind the virtual ones are always read LIVE from
        the device, even when the caller asked for cached values: the cache
        held stale pairing-time values (11 / 1000) that overwrote the real
        ones a few minutes after a restart.
        """
        real, virt = [], []
        for attr in attributes:
            attr_id = self.find_attribute(attr).id
            (virt if attr_id in _VIRTUAL else real).append(attr)
        targets = []
        for attr in virt:
            target = _VIRTUAL[self.find_attribute(attr).id]
            if target not in targets:
                targets.append(target)
        # real attributes that are also read live are served from that read
        also_live = [a for a in real if self.find_attribute(a).id in targets]
        real = [a for a in real if a not in also_live]
        success, failure = (
            await super().read_attributes(real, *args, **kwargs) if real else ({}, {})
        )
        if targets:
            await self._read_live(targets)
        for attr in also_live + virt:
            value = self.get(self.find_attribute(attr).id)
            if value is None:
                failure[attr] = foundation.Status.UNSUPPORTED_ATTRIBUTE
            else:
                success[attr] = value
        return success, failure


class TS110EOnOff(CustomCluster, OnOff):
    """OnOff with the Tuya power-on behavior."""

    class AttributeDefs(OnOff.AttributeDefs):
        """Attribute definitions."""

        power_on_behavior: Final = ZCLAttributeDef(id=0x8002, type=PowerOnState)


builder = QuirkBuilder("_TZ3210_3mpwqzuu", "TS110E")
for _ep in (1, 2):
    builder = (
        builder.replaces(TS110ELevelControl, endpoint_id=_ep)
        .replaces(TS110EOnOff, endpoint_id=_ep)
        .number(
            "min_brightness",
            LevelControl.cluster_id,
            endpoint_id=_ep,
            min_value=1,
            max_value=100,
            step=1,
            unit="%",
            mode="box",
            translation_key=f"minimum_brightness_l{_ep}",
            fallback_name=f"Brilho mínimo (canal {_ep})",
            # virtual: read 0xFC03 at startup instead of trusting the cache
            attribute_initialized_from_cache=False,
        )
        .number(
            "max_brightness",
            LevelControl.cluster_id,
            endpoint_id=_ep,
            min_value=1,
            max_value=100,
            step=1,
            unit="%",
            mode="box",
            translation_key=f"maximum_brightness_l{_ep}",
            fallback_name=f"Brilho máximo (canal {_ep})",
            # virtual: read 0xFC04 at startup instead of trusting the cache
            attribute_initialized_from_cache=False,
        )
        .enum(
            "switch_type",
            SwitchType,
            LevelControl.cluster_id,
            endpoint_id=_ep,
            translation_key=f"switch_type_l{_ep}",
            fallback_name=f"Tipo de interruptor (canal {_ep})",
        )
        .enum(
            "power_on_behavior",
            PowerOnState,
            OnOff.cluster_id,
            endpoint_id=_ep,
            translation_key=f"power_on_behavior_l{_ep}",
            fallback_name=f"Ao ligar a energia (canal {_ep})",
        )
    )
builder.add_to_registry()
