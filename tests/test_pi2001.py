"""pi2001 timezone, day boundary, failed eco results, and stale bowl cache.

The numbers here are fixtures. Eaten grams come only from the eco result.
They are not derived from bowl weight.
"""

from __future__ import annotations

import copy
import datetime as dt
from types import SimpleNamespace
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaomi_pet_feeder.api import XiaomiApiError, device_midnight, device_now
from custom_components.xiaomi_pet_feeder.api.client import api_base
from custom_components.xiaomi_pet_feeder.api.feeder import (
    BOWL_CACHE_MAX_AGE,
    ECO_API,
    POLLED_PROPS,
    FeederApi,
    FeederStatus,
    Prop,
    current_bowl_grams,
    parse_device_tz_offset,
)
from custom_components.xiaomi_pet_feeder.commands import device_minutes
from custom_components.xiaomi_pet_feeder.const import CONF_DEVICES, CONF_SERVER, DOMAIN

from .conftest import DID, ENTRY_DATA, OWNER, FakeCloud
from .test_init import eid, setup

EATEN = f"{ECO_API}/getHasEatRecord"
FEED = f"{ECO_API}/getFeedRecord"
TAIPEI = 8 * 3600
ACTION = "/miotspec/action"
PROP_SET = "/miotspec/prop/set"


def _freeze(monkeypatch: pytest.MonkeyPatch, moment: dt.datetime) -> None:
    real = dt.datetime

    class Frozen(real):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            if tz is None:
                return moment.replace(tzinfo=None)
            return moment.astimezone(tz)

    monkeypatch.setattr(
        "custom_components.xiaomi_pet_feeder.api.feeder.dt.datetime",
        Frozen,
    )


def _pi_entry() -> MockConfigEntry:
    data = copy.deepcopy(ENTRY_DATA)
    data[CONF_SERVER] = "tw"
    data[CONF_DEVICES][0]["model"] = "xiaomi.feeder.pi2001"
    return MockConfigEntry(
        domain=DOMAIN,
        title="user@example.com (tw)",
        unique_id=f"{OWNER}_tw",
        data=data,
    )


class _Stub:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(self, path: str, data: dict[str, Any]) -> Any:
        self.calls.append((path, data))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_region_tw_only_changes_the_host() -> None:
    """pi2001 on the Taiwan server uses the same eco path as iv2001."""
    assert api_base("tw") == "https://tw.api.io.mi.com/app"
    assert api_base("cn") == "https://api.io.mi.com/app"
    assert "pi2001" not in ECO_API
    assert (2, 27) not in POLLED_PROPS
    assert Prop.TIMEZONE in POLLED_PROPS
    assert Prop.DST in POLLED_PROPS


def test_timezone_sign_range_and_explicit_utc() -> None:
    """5.17 is seconds east of UTC. Zero and west-of-UTC stay as reported."""
    assert parse_device_tz_offset(-10800) == -10800
    assert parse_device_tz_offset(0) == 0
    assert parse_device_tz_offset(TAIPEI) == TAIPEI
    assert parse_device_tz_offset(TAIPEI * 1000) is None
    assert parse_device_tz_offset(True) is None
    assert parse_device_tz_offset("28800") == TAIPEI

    explicit = FeederStatus.from_cloud(
        [{"siid": 5, "piid": 17, "code": 0, "value": 0}, {"siid": 5, "piid": 19, "code": 0, "value": 0}]
    )
    explicit.tz_fallback = TAIPEI
    assert explicit.timezone_source == "device"
    assert explicit.tz_offset == 0
    assert explicit.dst is False
    assert FeederApi._offsets(explicit.tz_offset, explicit.dst) == (0, 3600)

    west = FeederStatus.from_cloud([{"siid": 5, "piid": 17, "code": 0, "value": -10800}])
    west.tz_fallback = TAIPEI
    assert west.tz_offset == -10800
    assert FeederApi._offsets(-10800, True) == (-10800, -14400)
    assert FeederApi._offsets(-10800, False) == (-10800, -7200)


def test_missing_pi2001_timezone_uses_fallback_without_a_fake_dst_hour() -> None:
    """-4001 on 5.17/5.19 is unsupported, not UTC and not a one-hour shift."""
    status = FeederStatus.from_cloud(
        [
            {"siid": 5, "piid": 17, "code": -4001},
            {"siid": 5, "piid": 19, "code": -4001},
            {"siid": 2, "piid": 27, "code": -4001},
            {"siid": 2, "piid": 22, "code": 0, "value": 64, "updateTime": 1, "exe_time": 0},
        ]
    )
    assert Prop.TIMEZONE not in status.values
    assert Prop.DST not in status.values
    assert status.dst is None
    assert status.timezone_resolved is False
    status.tz_fallback = TAIPEI
    assert status.timezone_source == "home_assistant"
    assert status.tz_offset == TAIPEI
    assert FeederApi._offsets(status.tz_offset, status.dst) == (TAIPEI,)

    invalid = FeederStatus.from_cloud([{"siid": 5, "piid": 17, "code": 0, "value": TAIPEI * 1000}])
    invalid.tz_fallback = TAIPEI
    assert invalid.device_tz_offset is None
    assert invalid.tz_offset == TAIPEI


def test_day_boundaries_for_taipei_and_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """At 12:06 in UTC+8 the UTC query starts eight hours late; after 16:00 it is the wrong date."""
    noon = dt.datetime(2026, 11, 6, 4, 6, tzinfo=dt.UTC)
    _freeze(monkeypatch, noon)
    assert device_now(TAIPEI) == dt.datetime(2026, 11, 6, 12, 6)
    assert device_now(0) == dt.datetime(2026, 11, 6, 4, 6)
    taipei_midnight = device_midnight(TAIPEI)
    utc_midnight = device_midnight(0)
    assert dt.datetime.fromtimestamp(taipei_midnight, dt.UTC) == dt.datetime(2026, 11, 5, 16, tzinfo=dt.UTC)
    assert dt.datetime.fromtimestamp(utc_midnight, dt.UTC) == dt.datetime(2026, 11, 6, tzinfo=dt.UTC)
    assert utc_midnight - taipei_midnight == TAIPEI

    rollover = dt.datetime(2026, 11, 6, 20, 30, tzinfo=dt.UTC)
    _freeze(monkeypatch, rollover)
    assert device_now(TAIPEI).date() == dt.date(2026, 11, 7)
    assert device_now(0).date() == dt.date(2026, 11, 6)
    assert dt.datetime.fromtimestamp(device_midnight(TAIPEI), dt.UTC) == dt.datetime(
        2026, 11, 6, 16, tzinfo=dt.UTC
    )


def test_schedule_clock_refuses_an_unknown_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A schedule edit must not stamp entries with a guessed UTC clock."""
    status = FeederStatus({})
    feeder = SimpleNamespace(status=SimpleNamespace(data=status))
    with pytest.raises(ServiceValidationError) as err:
        device_minutes(feeder)
    assert err.value.translation_key == "timezone_unknown"

    _freeze(monkeypatch, dt.datetime(2026, 11, 6, 4, 6, tzinfo=dt.UTC))
    known = FeederStatus({}, tz_fallback=TAIPEI)
    feeder = SimpleNamespace(status=SimpleNamespace(data=known))
    assert device_minutes(feeder) == 12 * 60 + 6


def test_optimistic_write_keeps_the_resolved_timezone() -> None:
    """A local control update must not drop the timezone used for later reads."""
    status = FeederStatus.from_cloud([{"siid": 5, "piid": 17, "code": 0, "value": 0}])
    status.tz_fallback = TAIPEI
    updated = status.with_value(Prop.CHILD_LOCK, True)
    assert updated.tz_offset == 0
    assert updated.tz_fallback == TAIPEI
    assert updated.get(Prop.CHILD_LOCK) is True

    missing = FeederStatus({}, tz_fallback=TAIPEI)
    kept = missing.with_value(Prop.SCHEDULE, "[1,06000101]")
    assert kept.timezone_source == "home_assistant"
    assert kept.tz_offset == TAIPEI
    assert kept.get(Prop.SCHEDULE) == "[1,06000101]"


def test_bowl_cache_is_not_a_fresh_measurement() -> None:
    """Stored grams stay available as last reported, and are hidden once they are old."""
    updated = dt.datetime(2026, 11, 6, 6, 0, tzinfo=dt.UTC)
    status = FeederStatus.from_cloud(
        [
            {
                "siid": 2,
                "piid": 22,
                "code": 0,
                "value": 64,
                "updateTime": int(updated.timestamp()),
                "exe_time": 0,
            }
        ]
    )
    assert current_bowl_grams(status, updated + BOWL_CACHE_MAX_AGE) == 64
    assert current_bowl_grams(status, updated + BOWL_CACHE_MAX_AGE + dt.timedelta(seconds=1)) is None
    attrs = status.bowl_attributes(updated + BOWL_CACHE_MAX_AGE + dt.timedelta(seconds=1))
    assert attrs["origin"] == "cloud_cache"
    assert attrs["live"] is False
    assert attrs["stale"] is True
    assert attrs["last_reported_grams"] == 64

    recent_zero = FeederStatus.from_cloud(
        [{"siid": 2, "piid": 22, "code": 0, "value": 0, "updateTime": int(updated.timestamp()), "exe_time": 0}]
    )
    assert current_bowl_grams(recent_zero, updated) == 0
    assert recent_zero.bowl_attributes(updated)["live"] is False

    live = FeederStatus.from_cloud(
        [
            {
                "siid": 2,
                "piid": 22,
                "code": 0,
                "value": 3,
                "updateTime": 1,
                "exe_time": 40,
                "otlocalts": 1,
            }
        ]
    )
    assert current_bowl_grams(live, updated + dt.timedelta(days=30)) == 3
    assert live.bowl_attributes(updated)["live"] is True
    assert live.bowl_attributes(updated)["stale"] is False

    undated = FeederStatus.from_cloud([{"siid": 2, "piid": 22, "code": 0, "value": 64, "exe_time": 0}])
    assert current_bowl_grams(undated, updated) is None


async def test_failed_eco_result_is_not_zero_or_empty_history() -> None:
    """Null is a failed payload. Numeric 0 and an empty list are real empties."""
    api = FeederApi(_Stub(None), "did", "owner")  # type: ignore[arg-type]
    with pytest.raises(XiaomiApiError):
        await api.eaten_today(TAIPEI)
    with pytest.raises(XiaomiApiError):
        await api.records_today(TAIPEI, dst=None)
    with pytest.raises(XiaomiApiError):
        await api.hourly(TAIPEI, dt.date(2026, 11, 6))

    zero = FeederApi(_Stub(0), "did", "owner")  # type: ignore[arg-type]
    assert await zero.eaten_today(TAIPEI) == 0
    empty = FeederApi(_Stub([]), "did", "owner")  # type: ignore[arg-type]
    assert await empty.records_today(TAIPEI, dst=None) == []


async def test_pi2001_history_query_uses_one_taipei_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The eco body carries the fallback offset and that day's real midnight."""
    _freeze(monkeypatch, dt.datetime(2026, 11, 6, 4, 6, tzinfo=dt.UTC))
    client = _Stub([])
    api = FeederApi(client, "did", "owner")  # type: ignore[arg-type]
    assert await api.records_today(TAIPEI, dst=None) == []
    assert [call[0] for call in client.calls] == [FEED]
    assert client.calls[0][1]["timeZoneOffSetSeconds"] == TAIPEI
    assert client.calls[0][1]["startTime"] == device_midnight(TAIPEI)
    assert "pi2001" not in client.calls[0][0]


async def test_pi2001_cloud_read_does_not_write_or_invent_grams(
    hass: HomeAssistant, cloud: FakeCloud
) -> None:
    """Region tw, missing 5.17/5.19, stale bowl, and an eco total that is not the bowl."""
    await hass.config.async_set_time_zone("Asia/Taipei")
    cloud.hide_prop(5, 17)
    cloud.hide_prop(5, 19)
    cloud.set_prop_fields(2, 22, value=64, updateTime=1, exe_time=0)
    cloud.responses["eaten_today"] = {"code": 0, "message": "sucess", "result": 5}
    entry = _pi_entry()
    await setup(hass, entry)

    assert hass.states.get(eid(hass, "sensor", "eaten_today")).state == "5"
    bowl = hass.states.get(eid(hass, "sensor", "bowl_food"))
    # Unknown, not the stored grams: HA omits attributes while unavailable.
    assert bowl.state == "unknown"
    assert bowl.attributes["last_reported_grams"] == 64
    assert bowl.attributes["stale"] is True
    assert bowl.attributes["live"] is False
    assert bowl.attributes["origin"] == "cloud_cache"

    eaten = cloud.calls_to(EATEN)
    assert eaten[0]["timeZoneOffSetSeconds"] == TAIPEI
    assert eaten[0]["startTime"] == device_midnight(TAIPEI)
    feed_offsets = {call["timeZoneOffSetSeconds"] for call in cloud.calls_to(FEED)}
    assert feed_offsets == {TAIPEI}
    assert cloud.calls_to(PROP_SET) == []
    assert cloud.calls_to(ACTION) == []

    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, DID), entry.entry_id)
    assert device is not None
    assert device.model_id == "xiaomi.feeder.pi2001"
    schedule = await hass.services.async_call(
        DOMAIN,
        "get_schedule",
        {"device_id": device.id},
        blocking=True,
        return_response=True,
    )
    assert [row["time"] for row in schedule["entries"]] == ["06:00", "10:00", "16:00", "20:00"]
    assert cloud.calls_to(ACTION) == []


async def test_live_bowl_is_shown_even_when_its_timestamp_is_old(
    hass: HomeAssistant, cloud: FakeCloud
) -> None:
    """A device read (otlocalts / exe_time) is the current bowl, whatever the cache age."""
    await hass.config.async_set_time_zone("Asia/Taipei")
    cloud.hide_prop(5, 17)
    cloud.hide_prop(5, 19)
    cloud.set_prop_fields(2, 22, value=3, updateTime=1, exe_time=40, otlocalts=1)
    entry = _pi_entry()
    await setup(hass, entry)
    bowl = hass.states.get(eid(hass, "sensor", "bowl_food"))
    assert bowl.state == "3"
    assert bowl.attributes["live"] is True
    assert bowl.attributes["stale"] is False
    assert bowl.attributes["origin"] == "device"


async def test_explicit_utc_device_is_not_replaced_by_taipei(
    hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud
) -> None:
    """iv2001 reporting 0 and standard time keeps UTC, including the seasonal second query."""
    await hass.config.async_set_time_zone("Asia/Taipei")
    cloud.set_prop_fields(5, 17, value=0)
    cloud.set_prop_fields(5, 19, value=0)
    await setup(hass, entry)
    assert cloud.calls_to(EATEN)[0]["timeZoneOffSetSeconds"] == 0
    assert {call["timeZoneOffSetSeconds"] for call in cloud.calls_to(FEED)} == {0, 3600}


async def test_numeric_zero_and_empty_history_are_real(
    hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud
) -> None:
    """A cloud 0 and an empty list stay 0 and empty."""
    cloud.responses["eaten_today"] = {"code": 0, "message": "sucess", "result": 0}
    cloud.responses["feed_record"] = {"code": 0, "message": "sucess", "result": []}
    await setup(hass, entry)
    assert hass.states.get(eid(hass, "sensor", "eaten_today")).state == "0"
    assert hass.states.get(eid(hass, "sensor", "feeding_log")).state == "0"


async def test_null_history_does_not_publish_a_zero_day(
    hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud
) -> None:
    """A null record list fails the update instead of showing an empty day."""
    cloud.responses["eaten_today"] = {"code": 0, "message": "sucess", "result": 0}
    cloud.responses["feed_record"] = {"code": 0, "message": "sucess", "result": None}
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not any(state.entity_id.endswith("eaten_today") for state in hass.states.async_all())


async def test_unknown_timezone_does_not_query_as_utc(
    hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud
) -> None:
    """After setup, dropping both timezone sources does not send offset 0."""
    await setup(hass, entry)
    feeder = entry.runtime_data.feeders[DID]
    before = len(cloud.calls_to(EATEN))
    feeder.status.data.values.pop(Prop.TIMEZONE)
    feeder.status.data.tz_fallback = None
    with pytest.raises(UpdateFailed):
        await feeder.records._async_update_data()
    assert len(cloud.calls_to(EATEN)) == before
