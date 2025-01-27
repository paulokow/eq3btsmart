import datetime

from .python_eq3bt.eq3bt.structures import (
    HOUR_24_PLACEHOLDER,
)
from .const import DOMAIN
import logging

import voluptuous as vol
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import format_mac
from .python_eq3bt.eq3bt.eq3btsmart import EQ3BT_MAX_TEMP, EQ3BT_MIN_TEMP, Thermostat
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def times_and_temps_schema(value):
    """Validate times."""

    def v_assert(bool, error):
        if not bool:
            raise vol.Invalid(error)

    time = lambda i: value.get(f"next_change_at_{i}")
    temp = lambda i: value.get(f"target_temp_{i}")

    v_assert(temp(0), f"Missing target_temp_{0}")
    if time(0):
        v_assert(temp(1), f"Missing target_temp_{1} after: {time(0)}")
    for i in range(1, 7):
        if time(i):
            v_assert(time(i - 1), f"Missing next_change_at_{i-1} before: {time(i)}")
            v_assert(
                time(i - 1) < time(i),
                f"Times not in order at next_change_at_{i}: {time(i-1)}≥{time(i)}",
            )
            v_assert(temp(i + 1), f"Missing target_temp_{i+1} after: {time(i)}")
        if temp(i):
            v_assert(temp(i - 1), f"Missing target_temp_{i-1} before: {time(i-1)}")
            v_assert(time(i - 1), f"Missing next_change_at_{i-1} after: {time(i-2)}")
    return value


EQ3_TEMPERATURE = vol.Range(min=EQ3BT_MIN_TEMP, max=EQ3BT_MAX_TEMP)

SCHEDULE_SCHEMA = {
    vol.Required("days"): cv.weekdays,
    vol.Required("target_temp_0"): EQ3_TEMPERATURE,
    vol.Optional("next_change_at_0"): cv.time,
    vol.Optional("target_temp_1"): EQ3_TEMPERATURE,
    vol.Optional("next_change_at_1"): cv.time,
    vol.Optional("target_temp_2"): EQ3_TEMPERATURE,
    vol.Optional("next_change_at_2"): cv.time,
    vol.Optional("target_temp_3"): EQ3_TEMPERATURE,
    vol.Optional("next_change_at_3"): cv.time,
    vol.Optional("target_temp_4"): EQ3_TEMPERATURE,
    vol.Optional("next_change_at_4"): cv.time,
    vol.Optional("target_temp_5"): EQ3_TEMPERATURE,
    vol.Optional("next_change_at_5"): cv.time,
    vol.Optional("target_temp_6"): EQ3_TEMPERATURE,
}

SET_SCHEDULE_SCHEMA = vol.All(
    cv.make_entity_service_schema(SCHEDULE_SCHEMA),
    times_and_temps_schema,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add sensors for passed config_entry in HA."""
    eq3 = hass.data[DOMAIN][config_entry.entry_id]

    new_devices = [
        FetchScheduleButton(eq3),
        FetchButton(eq3),
    ]
    async_add_entities(new_devices)

    platform = entity_platform.async_get_current_platform()

    platform.async_register_entity_service(
        "set_schedule",
        SET_SCHEDULE_SCHEMA,  # type: ignore
        "set_schedule",
    )


class Base(ButtonEntity):
    """Representation of an eQ-3 Bluetooth Smart thermostat."""

    def __init__(self, _thermostat: Thermostat):
        self._thermostat = _thermostat
        self._attr_has_entity_name = True

    @property
    def unique_id(self) -> str:
        assert self.name
        return format_mac(self._thermostat.mac) + "_" + self.name

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._thermostat.mac)},
        )


class FetchScheduleButton(Base):
    def __init__(self, _thermostat: Thermostat):
        super().__init__(_thermostat)
        _thermostat.register_update_callback(self.schedule_update_ha_state)
        self._attr_name = "Fetch Schedule"

    async def async_press(self) -> None:
        await self.fetch_schedule()

    async def fetch_schedule(self):
        for x in range(0, 7):
            await self._thermostat.async_query_schedule(x)
        _LOGGER.debug(
            "[%s] schedule (day %s): %s",
            self._thermostat.name,
            self._thermostat.schedule,
        )

    async def set_schedule(self, **kwargs) -> None:
        _LOGGER.debug("[%s] set_schedule (day %s)", self._thermostat.name, kwargs)
        for day in kwargs["days"]:
            times = [
                kwargs.get(f"next_change_at_{i}", datetime.time(0, 0)) for i in range(6)
            ]
            times[times.index(datetime.time(0, 0))] = HOUR_24_PLACEHOLDER
            temps = [kwargs.get(f"target_temp_{i}", 0) for i in range(7)]
            hours = []
            for i in range(0, 6):
                hours.append(
                    {
                        "target_temp": temps[i],
                        "next_change_at": times[i],
                    }
                )
            await self._thermostat.async_set_schedule(day=day, hours=hours)

    @property
    def extra_state_attributes(self):
        schedule = {}
        for day in self._thermostat.schedule:
            day_raw = self._thermostat.schedule[day]
            day_nice = {"day": day}
            for i, entry in enumerate(day_raw.hours):
                day_nice[f"target_temp_{i}"] = entry.target_temp
                if entry.next_change_at == HOUR_24_PLACEHOLDER:
                    break
                day_nice[f"next_change_at_{i}"] = entry.next_change_at.isoformat()
            schedule[day] = day_nice

        return schedule


class FetchButton(Base):
    def __init__(self, _thermostat: Thermostat):
        super().__init__(_thermostat)
        self._attr_name = "Fetch"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        await self._thermostat.async_update()
