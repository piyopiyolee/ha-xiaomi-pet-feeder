"""High level API of the Xiaomi Smart Pet Food Feeder 2 (iv2001 / pi2001).

Property and action ids come from the MIoT spec and from the official Mi Home
plugin (``com.midr.pet.feeder``), which is what the app actually uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
from enum import StrEnum
from typing import Any

from .client import XiaomiCloudClient
from .exceptions import XiaomiApiError
from .schedule import GRAMS_PER_PORTION, FeedingSchedule

SUPPORTED_MODELS = ("xiaomi.feeder.iv2001", "xiaomi.feeder.pi2001")

# Both models call the same plugin service. pi2001 has no separate eco path.
ECO_API = "/eco/pet-feeder-new/petFeederEventApi"
ACCESS_KEY = "IOS00026747c5acafc2"

# MIoT device-timezone (iv2001 5.17) is seconds east of UTC, inclusive.
TZ_OFFSET_MIN = -43_200
TZ_OFFSET_MAX = 50_400
# A stored bowl reading older than this is not the current weight.
BOWL_CACHE_MAX_AGE = dt.timedelta(hours=6)
# Feed events carry this offset in piid 27 when the device has no 5.17.
EVENT_OFFSET_DEFAULT = 0


class Prop:
    """(siid, piid) of the properties used by the app."""

    FIRMWARE = (1, 4)
    SENSOR_FAULT = (2, 1)
    FOOD_LOW = (2, 6)
    FOOD_STUCK = (2, 10)
    BOWL_ABNORMAL = (2, 11)
    REFILL_DUE = (2, 13)
    BOWL_HEAP = (2, 15)
    BOWL_WEIGHT = (2, 22)
    DISPENSING = (2, 26)
    LOW_INTAKE = (2, 32)
    CHILD_LOCK = (3, 1)
    AUTO_SCREEN_OFF = (3, 3)
    BATTERY_LOW = (4, 1)
    SCHEDULE = (5, 1)
    REFILL_REMINDER = (5, 3)
    SCHEDULE_DISPLAY = (5, 4)
    LOW_INTAKE_THRESHOLD = (5, 5)
    LOW_INTAKE_NOTIFY = (5, 6)
    EMPTY_BOWL_HOURS = (5, 10)
    DISPENSE_PROGRESS = (5, 11)
    COMPENSATION = (5, 12)
    PREVENT_HEAP = (5, 14)
    SCHEDULE_PROGRESS = (5, 15)
    REFILL_TRIGGER_HOURS = (5, 16)
    # iv2001 only. pi2001 v1 and v3 do not define 5.17 or 5.19. pi2001 2.27 is
    # an unread event argument (uint32, empty access, local read -4001), not
    # a substitute for 5.17, and is intentionally not polled.
    TIMEZONE = (5, 17)
    SCREEN_DISPLAY = (5, 18)
    DST = (5, 19)


POLLED_PROPS: list[tuple[int, int]] = [
    v for k, v in vars(Prop).items() if not k.startswith("_") and isinstance(v, tuple)
]


class Action:
    """(siid, aiid) of the actions used by the app."""

    DISPENSE = (2, 1)  # in: [portions]
    CALIBRATE = (2, 2)  # in: []
    LOW_INTAKE_SETTINGS = (5, 1)  # in: [threshold %, notify bool]
    WRITE_SCHEDULE = (5, 2)  # in: [schedule string]
    REFILL_SETTINGS = (5, 3)  # in: [reminder bool, empty bowl hours]
    SCHEDULE_DISPLAY = (5, 4)  # in: [bool]
    CLEAR_LOW_INTAKE = (5, 5)  # in: [0]
    CLEAR_REFILL = (5, 6)  # in: [0]
    RESET_DESICCANT = (6, 1)  # in: []


class FeedSource(StrEnum):
    """Who triggered a dispense (units digit of event piid 16)."""

    APP = "app"
    BUTTON = "button"
    SCHEDULE = "schedule"
    EAT = "eat"
    UNKNOWN = "unknown"


class FeedResult(StrEnum):
    """Outcome of a dispense (tens digit of event piid 16)."""

    OK = "ok"
    FOOD_STUCK = "food_stuck"
    OUT_OF_FOOD = "out_of_food"
    BOWL_FULL = "bowl_full"
    OTA = "interrupted_by_update"
    FAILED = "failed"
    UNKNOWN = "unknown"


_SOURCES = {0: FeedSource.APP, 1: FeedSource.BUTTON, 2: FeedSource.SCHEDULE, 3: FeedSource.EAT}
_RESULTS = {
    0: FeedResult.OK,
    1: FeedResult.FOOD_STUCK,
    2: FeedResult.OUT_OF_FOOD,
    3: FeedResult.BOWL_FULL,
    4: FeedResult.OTA,
    5: FeedResult.FAILED,
}


class DesiccantState(StrEnum):
    """Desiccant cartridge state as shown by the app."""

    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"


_DESICCANT = {
    1: DesiccantState.SUFFICIENT,
    3: DesiccantState.INSUFFICIENT,
    4: DesiccantState.EXHAUSTED,
}


@dataclass(frozen=True, slots=True)
class FeedRecord:
    """A meal (pet ate) or a dispense, as stored in the cloud."""

    ts: int
    kind: str  # "meal" | "dispense"
    grams: int | None = None
    portions: int | None = None
    source: FeedSource | None = None
    result: FeedResult | None = None

    @property
    def time(self) -> dt.datetime:
        """UTC timestamp."""
        return dt.datetime.fromtimestamp(self.ts, dt.UTC)

    def as_dict(self) -> dict[str, Any]:
        """Serializable form for attributes and service responses."""
        data: dict[str, Any] = {"time": self.time.isoformat(), "type": self.kind}
        if self.kind == "meal":
            data["grams"] = self.grams
        else:
            data |= {
                "portions": self.portions,
                "source": self.source,
                "result": self.result,
            }
        return data

    @property
    def key(self) -> tuple[int, str]:
        """Identity used to detect new records."""
        return (self.ts, self.kind)


def parse_record(row: dict[str, Any]) -> FeedRecord | None:
    """Turn a petFeederEventApi row into a record; unknown events are dropped."""
    params = {p.get("piid"): p.get("value") for p in row.get("params") or []}
    ts = int(row.get("ts") or 0)
    eiid = row.get("eiid")
    try:
        if eiid == 1:
            return FeedRecord(ts=ts, kind="meal", grams=int(params.get(18) or 0))
        if eiid in (3, 5):
            code = int(params.get(16) or 0)
            amount = int(params.get(8) or 0)
            return FeedRecord(
                ts=ts,
                kind="dispense",
                portions=round(amount / GRAMS_PER_PORTION),
                source=_SOURCES.get(code % 10, FeedSource.UNKNOWN),
                result=_RESULTS.get(code // 10, FeedResult.UNKNOWN),
            )
    except TypeError, ValueError:
        return None
    return None


@dataclass(slots=True)
class Desiccant:
    """Desiccant cartridge info from the consumables API."""

    state: DesiccantState
    level: int | None = None
    hours_left: int | None = None
    total_hours: int | None = None


@dataclass(frozen=True, slots=True)
class PropertyReading:
    """One MIoT property result, including a failed or cached read."""

    value: Any
    code: int
    update_time: int | None
    exe_time: int | None
    live: bool

    @property
    def ok(self) -> bool:
        """True when the gateway returned a value with code 0."""
        return self.code == 0 and self.value is not None

    def is_stale(self, now: dt.datetime, max_age: dt.timedelta) -> bool:
        """Return whether a stored reading is too old to show as current."""
        if self.code != 0 or self.value is None:
            return True
        if self.live:
            return False
        if self.update_time is None:
            return True
        updated = dt.datetime.fromtimestamp(self.update_time, dt.UTC)
        return now - updated > max_age


def parse_device_tz_offset(value: Any) -> int | None:
    """Return 5.17 as seconds east of UTC, or None when it is not that spec.

    The published range is -43200..50400 seconds. Values outside it (for
    example milliseconds) are rejected instead of being rescaled. Zero is a
    real UTC offset, not a missing value. Booleans are not numbers here.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, int) and TZ_OFFSET_MIN <= value <= TZ_OFFSET_MAX:
        return value
    return None


def _as_grams(value: Any) -> int | None:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        return None
    if isinstance(value, int | float):
        return int(value)
    return None


def current_bowl_grams(status: FeederStatus, now: dt.datetime) -> int | None:
    """Grams to show as the current bowl weight, or None when that would be stale."""
    reading = status.readings.get(Prop.BOWL_WEIGHT)
    if reading is None or reading.is_stale(now, BOWL_CACHE_MAX_AGE):
        return None
    return _as_grams(reading.value)


@dataclass(slots=True)
class FeederStatus:
    """Snapshot of the MIoT properties."""

    values: dict[tuple[int, int], Any] = field(default_factory=dict)
    readings: dict[tuple[int, int], PropertyReading] = field(default_factory=dict)
    tz_fallback: int | None = None

    @classmethod
    def from_cloud(cls, items: list[dict[str, Any]]) -> FeederStatus:
        """Build a snapshot from ``/miotspec/prop/get`` results.

        ``code == 0`` supplies a value. Any other code, including -4001
        (property missing or not readable), is remembered and is not a value.
        """
        values: dict[tuple[int, int], Any] = {}
        readings: dict[tuple[int, int], PropertyReading] = {}
        for item in items:
            try:
                key = (int(item["siid"]), int(item["piid"]))
            except (KeyError, TypeError, ValueError):
                continue
            code = _optional_int(item.get("code", 0))
            if code is None:
                code = -1
            exe_time = _optional_int(item.get("exe_time"))
            update_time = _optional_int(item.get("updateTime"))
            live = item.get("otlocalts") is not None or (exe_time is not None and exe_time > 0)
            present = code == 0 and "value" in item
            readings[key] = PropertyReading(
                value=item.get("value") if present else None,
                code=code,
                update_time=update_time,
                exe_time=exe_time,
                live=live,
            )
            if present:
                values[key] = item["value"]
        return cls(values, readings)

    def get(self, prop: tuple[int, int], default: Any = None) -> Any:
        """Raw property value."""
        return self.values.get(prop, default)

    def flag(self, prop: tuple[int, int]) -> bool | None:
        """Property as a boolean (0/1 or bool)."""
        value = self.values.get(prop)
        return None if value is None else bool(int(value))

    @property
    def schedule(self) -> FeedingSchedule:
        """Parsed feeding plan."""
        return FeedingSchedule.parse(self.values.get(Prop.SCHEDULE))

    @property
    def device_tz_offset(self) -> int | None:
        """5.17 in seconds, or None when the device did not report a usable one."""
        if Prop.TIMEZONE not in self.values:
            return None
        return parse_device_tz_offset(self.values[Prop.TIMEZONE])

    @property
    def timezone_source(self) -> str:
        """``device``, ``home_assistant`` or ``unknown``."""
        if self.device_tz_offset is not None:
            return "device"
        if self.tz_fallback is not None:
            return "home_assistant"
        return "unknown"

    @property
    def timezone_resolved(self) -> bool:
        """False when neither the device nor Home Assistant provides an offset."""
        return self.timezone_source != "unknown"

    @property
    def tz_offset(self) -> int:
        """UTC offset in seconds. Explicit device 0 stays 0 and is not replaced."""
        device = self.device_tz_offset
        if device is not None:
            return device
        if self.tz_fallback is not None:
            return self.tz_fallback
        return 0

    @property
    def dst(self) -> bool | None:
        """5.19 when the device reports it. None means the property is absent.

        False is an explicit standard-time report (iv2001) and still selects
        the plugin's two seasonal queries. None (pi2001) does not invent one.
        """
        if Prop.DST not in self.values:
            return None
        value = self.values[Prop.DST]
        if isinstance(value, bool):
            return value
        try:
            return bool(int(value))
        except (TypeError, ValueError):
            return None

    def with_value(self, prop: tuple[int, int], value: Any) -> FeederStatus:
        """Copy with one optimistic value, keeping timezone fallback and reads."""
        values = dict(self.values)
        values[prop] = value
        readings = dict(self.readings)
        readings[prop] = PropertyReading(
            value=value, code=0, update_time=None, exe_time=1, live=True
        )
        return FeederStatus(values, readings, self.tz_fallback)

    def property_cache(self) -> dict[str, Any]:
        """Summarize cloud property age. An old stamp is not a replaced device."""
        stamps = [
            reading.update_time for reading in self.readings.values() if reading.update_time
        ]
        newest = max(stamps) if stamps else None
        return {
            "live_count": sum(
                1 for reading in self.readings.values() if reading.ok and reading.live
            ),
            "cached_count": sum(
                1 for reading in self.readings.values() if reading.ok and not reading.live
            ),
            "newest_update": None
            if newest is None
            else dt.datetime.fromtimestamp(newest, dt.UTC).isoformat(),
        }

    def bowl_attributes(self, now: dt.datetime) -> dict[str, Any]:
        """Origin of the bowl reading. A cache is never described as live."""
        reading = self.readings.get(Prop.BOWL_WEIGHT)
        if reading is None:
            return {}
        updated = None
        if reading.update_time is not None:
            updated = dt.datetime.fromtimestamp(reading.update_time, dt.UTC).isoformat()
        data: dict[str, Any] = {
            "origin": "device" if reading.live else "cloud_cache",
            "live": reading.live,
            "stale": reading.is_stale(now, BOWL_CACHE_MAX_AGE),
            "last_reported_grams": _as_grams(reading.value) if reading.ok else None,
        }
        if updated is not None:
            data["updated_at"] = updated
        return data


def device_now(tz_offset: int) -> dt.datetime:
    """Return the current wall time of the device as a naive datetime."""
    return (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=tz_offset)).replace(tzinfo=None)


def query_offsets(
    tz_offset: int, dst: bool | None, *, event_default: bool = False
) -> tuple[int, ...]:
    """Return eco partition keys for one local day.

    iv2001 with a reported 5.19 still adds the other seasonal hour. pi2001 has
    no 5.17, so a Home Assistant fallback also asks for 0: public feed events
    store that value in piid 27 and in ``timeZoneOffSet`` when the device zone
    is not what was uploaded. The wall-clock ``startTime`` stays on ``tz_offset``.
    """
    offsets = [tz_offset]
    if dst is not None:
        offsets.append(tz_offset + (-3600 if dst else 3600))
    if event_default and EVENT_OFFSET_DEFAULT not in offsets:
        offsets.append(EVENT_OFFSET_DEFAULT)
    return tuple(offsets)


@dataclass(frozen=True, slots=True)
class EatenToday:
    """Cloud total for the local day.

    ``grams is None`` when every partition returned a numeric 0 or an empty
    history and the offset was not reported by the device. That is not a
    verified intake of 0 g.
    """

    grams: int | None
    confirmed: bool
    offset: int | None
    offsets: tuple[int, ...]


def device_midnight(tz_offset: int, day: dt.date | None = None) -> int:
    """Unix timestamp of 00:00 device time for ``day`` (default: today)."""
    day = day or device_now(tz_offset).date()
    midnight = dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC)
    return int(midnight.timestamp()) - tz_offset


class FeederApi:
    """Operations on one feeder."""

    def __init__(self, client: XiaomiCloudClient, did: str, owner_id: str) -> None:
        """Bind to a device; ``owner_id`` is the Xiaomi id of the device owner."""
        self.client = client
        self.did = did
        self.owner_id = owner_id

    async def status(self) -> FeederStatus:
        """Read every property the integration exposes."""
        return FeederStatus.from_cloud(await self.client.get_properties(self.did, POLLED_PROPS))

    async def set_property(self, prop: tuple[int, int], value: Any) -> None:
        """Write a property."""
        await self.client.set_property(self.did, prop[0], prop[1], value)

    async def action(self, action: tuple[int, int], *ins: Any) -> None:
        """Run an action."""
        await self.client.action(self.did, action[0], action[1], list(ins))

    # -------------------------------------------------------- eco service
    def _eco_base(self, tz_offset: int) -> dict[str, Any]:
        return {
            "userId": self.owner_id,
            "deviceId": self.did,
            "timeZoneOffSetSeconds": tz_offset,
            "accessKey": ACCESS_KEY,
        }

    async def eaten_today(
        self,
        tz_offset: int,
        dst: bool | None = None,
        *,
        event_default: bool = False,
        confirmed: bool = True,
    ) -> EatenToday:
        """Grams eaten since local midnight.

        A numeric 0 from the device's own offset is a real zero. The same 0
        from only a fallback offset is not. A null result is still an error.
        """
        path = f"{ECO_API}/getHasEatRecord"
        offsets = query_offsets(tz_offset, dst, event_default=event_default)
        start = device_midnight(tz_offset)
        for offset in offsets:
            grams = _int_result(
                await self.client.request(
                    path,
                    self._eco_base(offset) | {"startTime": start},
                ),
                path,
            )
            if grams:
                return EatenToday(grams, True, offset, offsets)
        if confirmed:
            return EatenToday(0, True, tz_offset, offsets)
        return EatenToday(None, False, None, offsets)

    @staticmethod
    def _offsets(tz_offset: int, dst: bool | None) -> tuple[int, ...]:
        """Seasonal offsets only, without the pi2001 event-default partition."""
        return query_offsets(tz_offset, dst, event_default=False)

    async def records_today(
        self, tz_offset: int, dst: bool | None = None, *, event_default: bool = False
    ) -> list[FeedRecord]:
        """Meals and dispenses of today, oldest first."""
        rows: list[dict[str, Any]] = []
        path = f"{ECO_API}/getFeedRecord"
        start_of_day = device_midnight(tz_offset)
        for offset in query_offsets(tz_offset, dst, event_default=event_default):
            start = start_of_day
            for _ in range(10):
                page = _list_result(
                    await self.client.request(
                        path,
                        self._eco_base(offset) | {"startTime": start, "pageCount": 60},
                    ),
                    path,
                )
                rows.extend(page)
                if len(page) < 60:
                    break
                start = int(page[-1].get("ts") or start)
        return _dedupe(rows)

    async def records_between(
        self,
        tz_offset: int,
        start_ts: int,
        end_ts: int,
        dst: bool | None = None,
        limit: int = 2000,
        *,
        event_default: bool = False,
    ) -> list[FeedRecord]:
        """History between two timestamps (the cloud keeps 90 days), newest first."""
        rows: list[dict[str, Any]] = []
        path = f"{ECO_API}/multiGetFeedRecord"
        for offset in query_offsets(tz_offset, dst, event_default=event_default):
            cursor = end_ts
            fetched = 0
            while fetched < limit:
                page = _list_result(
                    await self.client.request(
                        path,
                        self._eco_base(offset) | {"startTime": cursor, "pageCount": 400},
                    ),
                    path,
                )
                fetched += len(page)
                rows.extend(r for r in page if start_ts <= int(r.get("ts") or 0) <= end_ts)
                if len(page) < 400 or int(page[-1].get("ts") or 0) < start_ts:
                    break
                cursor = int(page[-1]["ts"])
        records = _dedupe(rows)
        records.reverse()
        return records

    async def hourly(
        self,
        tz_offset: int,
        day: dt.date,
        *,
        event_default: bool = False,
        confirmed: bool = True,
    ) -> dict[str, Any]:
        """Hourly chart of one day: grams eaten and portions dispensed per hour."""
        path = f"{ECO_API}/getHourlyRecord"
        start = device_midnight(tz_offset, day)
        chosen: dict[str, Any] | None = None
        for offset in query_offsets(tz_offset, None, event_default=event_default):
            result = _object_result(
                await self.client.request(
                    path, self._eco_base(offset) | {"startTime": start}
                ),
                path,
            )
            chosen = _hourly_chart(day, result)
            if chosen["eaten_grams"] or chosen["dispensed_portions"]:
                chosen["intake_confirmed"] = True
                return chosen
        assert chosen is not None
        chosen["intake_confirmed"] = confirmed
        return chosen

    async def daily(
        self,
        tz_offset: int,
        start: dt.date,
        end: dt.date,
        *,
        event_default: bool = False,
        confirmed: bool = True,
    ) -> dict[str, Any]:
        """Daily chart: grams eaten and food left in the bowl at midnight."""
        path = f"{ECO_API}/getDailyRecord"
        body = {
            "startTime": device_midnight(tz_offset, start),
            "endTime": device_midnight(tz_offset, end) + 86399,
        }
        chosen: dict[str, Any] | None = None
        for offset in query_offsets(tz_offset, None, event_default=event_default):
            result = _object_result(
                await self.client.request(path, self._eco_base(offset) | body),
                path,
            )
            chosen = _daily_chart(start, end, result)
            if chosen["eaten_grams"] or chosen["left_in_bowl_grams"]:
                chosen["intake_confirmed"] = True
                return chosen
        assert chosen is not None
        chosen["intake_confirmed"] = confirmed
        return chosen

    async def desiccant(self, home_id: int, home_owner: int) -> Desiccant | None:
        """Desiccant state from the consumables API (MIoT 6.1/6.2 always read 0)."""
        result = await self.client.request(
            "/v2/home/standard_consumable_items",
            {
                "home_id": int(home_id),
                "owner_id": int(home_owner),
                "dids": [],
                "accessKey": ACCESS_KEY,
                "filter_ignore": True,
            },
        )
        for item in (result or {}).get("items") or []:
            for device in item.get("consumes_data") or []:
                if str(device.get("did")) != self.did:
                    continue
                for detail in device.get("details") or []:
                    if detail.get("prop") == "prop.6.1":
                        return Desiccant(
                            state=_DESICCANT.get(detail.get("state"), DesiccantState.UNKNOWN),
                            level=_int_or_none(detail.get("value")),
                            hours_left=_int_or_none(detail.get("left_time")),
                            total_hours=_int_or_none(detail.get("total_life")),
                        )
        return None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    return _optional_int(value)


def _int_result(result: Any, path: str) -> int:
    """Accept a real number, including 0. Reject null and other shapes."""
    if isinstance(result, bool) or result is None:
        raise XiaomiApiError(path, 0, "missing numeric result")
    if isinstance(result, int):
        return result
    if isinstance(result, float) and result.is_integer():
        return int(result)
    if isinstance(result, str):
        try:
            return int(result)
        except ValueError:
            pass
    raise XiaomiApiError(path, 0, "missing numeric result")


def _list_result(result: Any, path: str) -> list[Any]:
    """Accept a list, including an empty one. Null is not an empty history."""
    if isinstance(result, list):
        return result
    raise XiaomiApiError(path, 0, "missing list result")


def _object_result(result: Any, path: str) -> dict[str, Any]:
    """Accept an object, including an empty one. Null is not an empty chart."""
    if isinstance(result, dict):
        return result
    raise XiaomiApiError(path, 0, "missing object result")


def _hourly_chart(day: dt.date, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": day.isoformat(),
        "eaten_grams": {
            int(row["hour"]): int(row["param"] or 0)
            for row in result.get("hourlyEatFoodRecords") or []
        },
        "dispensed_portions": {
            int(row["hour"]): round(int(row["param"] or 0) / GRAMS_PER_PORTION)
            for row in result.get("hourlyOutFoodRecords") or []
        },
        "low_intake_percent": result.get("lowEatFoodParam"),
    }


def _daily_chart(start: dt.date, end: dt.date, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "eaten_grams": {
            row["date"]: int(row["param"] or 0) for row in result.get("dailyEatFoodRecords") or []
        },
        "left_in_bowl_grams": {
            row["date"]: int(row["param"] or 0) for row in result.get("dailyRestFoodRecords") or []
        },
    }


def _dedupe(rows: list[dict[str, Any]]) -> list[FeedRecord]:
    seen: set[tuple[int, str]] = set()
    records: list[FeedRecord] = []
    for row in rows:
        record = parse_record(row)
        if record is None or record.key in seen:
            continue
        seen.add(record.key)
        records.append(record)
    records.sort(key=lambda r: r.ts)
    return records
