"""Diagnostics for the Xiaomi Smart Pet Feeder."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONF_CLIENT_ID, CONF_CREDENTIALS
from .data import XiaomiPetFeederConfigEntry

TO_REDACT = {
    CONF_USERNAME,
    CONF_CLIENT_ID,
    CONF_CREDENTIALS,
    "did",
    "owner_id",
    "home_id",
    "home_owner",
    "name",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: XiaomiPetFeederConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics."""
    feeders = []
    for index, feeder in enumerate(entry.runtime_data.feeders.values()):
        status = feeder.status.data
        records = feeder.records.data
        feeders.append(
            {
                "index": index,
                "model": feeder.model,
                "properties": {
                    f"{s}.{p}": value
                    for (s, p), value in (status.values.items() if status else [])
                    if (s, p) not in ((1, 3), (1, 5))  # serial numbers
                },
                "timezone_source": status.timezone_source if status else None,
                "tz_offset_seconds": (
                    status.tz_offset if status and status.timezone_resolved else None
                ),
                "dst": status.dst if status else None,
                "bowl": status.bowl_attributes(dt_util.utcnow()) if status else None,
                "property_cache": status.property_cache() if status else None,
                "eaten_today": records.eaten_today if records else None,
                "intake_confirmed": records.intake_confirmed if records else None,
                "eco_offsets_seconds": list(records.offsets) if records else None,
                "summary_offset_seconds": records.summary_offset if records else None,
                "records_today": None
                if records is None or records.today is None
                else [item.as_dict() for item in records.today],
                "desiccant": repr(records.desiccant) if records else None,
                "status_update_ok": feeder.status.last_update_success,
                "records_update_ok": feeder.records.last_update_success,
                "official_events": len(feeder.push.entity_ids) if feeder.push else None,
            }
        )
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "feeders": feeders,
    }
