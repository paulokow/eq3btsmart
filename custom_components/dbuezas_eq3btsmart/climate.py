"""Support for dbuezas_eQ-3 Bluetooth Smart thermostats."""

from __future__ import annotations
import logging
from .python_eq3bt import eq3bt as eq3  # pylint: disable=import-error
from .const import PRESET_CLOSED, PRESET_NO_HOLD, PRESET_OPEN, PRESET_PERMANENT_HOLD
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers import entity_platform
from homeassistant.helpers import config_validation as cv
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_MAC,
    PRECISION_HALVES,
    TEMP_CELSIUS,
)
from homeassistant.components.climate.const import (
    HVAC_MODE_OFF,
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_NONE,
    SUPPORT_PRESET_MODE,
    SUPPORT_TARGET_TEMPERATURE,
)
from homeassistant.components.climate import HVACMode

from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity
import voluptuous as vol

from datetime import time, timedelta
from .python_eq3bt.eq3bt.eq3btsmart import (
    EQ3BT_MAX_TEMP,
    EQ3BT_OFF_TEMP,
)
from homeassistant.config_entries import ConfigEntry

from bleak.backends.device import BLEDevice

SCAN_INTERVAL = timedelta(minutes=5)
# PARALLEL_UPDATES = 0


def json_serial(obj):
    """JSON serializer for objects not serializable by default json code"""
    if isinstance(obj, time):
        return {"hour": obj.hour, "minute": obj.minute}
    # raise TypeError ("Type %s not serializable" % type(obj))
    return None


_LOGGER = logging.getLogger(__name__)

STATE_BOOST = "boost"

ATTR_STATE_WINDOW_OPEN = "window_open"
ATTR_STATE_VALVE = "valve"
ATTR_STATE_LOCKED = "is_locked"
ATTR_STATE_LOW_BAT = "low_battery"
ATTR_STATE_AWAY_END = "away_end"

EQ_TO_HA_HVAC = {
    eq3.Mode.Open: HVACMode.HEAT,
    eq3.Mode.Closed: HVACMode.OFF,
    eq3.Mode.Auto: HVACMode.AUTO,
    eq3.Mode.Manual: HVACMode.HEAT,
    eq3.Mode.Boost: HVACMode.AUTO,
    eq3.Mode.Away: HVACMode.HEAT,
}

HA_TO_EQ_HVAC = {
    HVACMode.HEAT: eq3.Mode.Manual,
    HVACMode.OFF: eq3.Mode.Closed,
    HVACMode.AUTO: eq3.Mode.Auto,
}

EQ_TO_HA_PRESET = {
    eq3.Mode.Boost: PRESET_BOOST,
    eq3.Mode.Away: PRESET_AWAY,
    eq3.Mode.Manual: PRESET_PERMANENT_HOLD,
    eq3.Mode.Auto: PRESET_NO_HOLD,
    eq3.Mode.Open: PRESET_OPEN,
    eq3.Mode.Closed: PRESET_CLOSED,
}

PRESET_FORCE_UPDATE = "Force update"  # fake preset to force fetching from thermostat

HA_TO_EQ_PRESET = {
    PRESET_BOOST: eq3.Mode.Boost,
    PRESET_AWAY: eq3.Mode.Away,
    PRESET_PERMANENT_HOLD: eq3.Mode.Manual,
    PRESET_NO_HOLD: eq3.Mode.Auto,
    PRESET_OPEN: eq3.Mode.Open,
    PRESET_CLOSED: eq3.Mode.Closed,
    PRESET_FORCE_UPDATE: None,
}


DEVICE_SCHEMA = vol.Schema({vol.Required(CONF_MAC): cv.string})

SUPPORT_FLAGS = SUPPORT_TARGET_TEMPERATURE | SUPPORT_PRESET_MODE


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add cover for passed entry in HA."""
    eq3 = EQ3BTSmartThermostat(entry.data["mac"], entry.data["name"], hass)
    devices = []
    devices.append(eq3)
    async_add_entities(
        devices,
        update_before_add=True,
    )

    platform = entity_platform.async_get_current_platform()

    platform.async_register_entity_service(
        "fetch_serial",
        {},
        EQ3BTSmartThermostat.fetch_serial.__name__,
    )

    platform.async_register_entity_service(
        "fetch_schedule",
        {},
        EQ3BTSmartThermostat.fetch_schedule.__name__,
    )
    platform.async_register_entity_service(
        "set_schedule",
        {},
        EQ3BTSmartThermostat.set_schedule.__name__,
    )


class EQ3BTSmartThermostat(ClimateEntity):
    """Representation of an eQ-3 Bluetooth Smart thermostat."""

    def __init__(self, _mac: str, _name: str, _hass: HomeAssistant):
        """Initialize the thermostat."""
        # We want to avoid name clash with this module.
        self.hass = _hass
        self._name = _name
        self._mac = _mac
        self._current_temperature = None
        self._is_setting_temperature = False
        self._thermostat = eq3.Thermostat(
            _mac,
            _name,
            _hass,
            lambda: self.schedule_update_ha_state(force_refresh=False),
        )
        # HA forces an update after any prop is set (temp, mode, etc)
        # But each time anything is set, the thermostat responds with the most current data
        # This means after setting a prop, we can skip the next scheduled update.
        self._skip_next_update = False
        self._is_first_update = True
        self.async_on_remove(self.on_remove)

    def on_remove(self):
        # TODO: can I cancel any running connection?
        _LOGGER.debug("[%s] removing", self.name)

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return SUPPORT_FLAGS

    @property
    def available(self) -> bool:
        """Return if thermostat is available."""
        return self._thermostat.mode >= 0

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def temperature_unit(self):
        """Return the unit of measurement that is used."""
        return TEMP_CELSIUS

    @property
    def precision(self):
        """Return eq3bt's precision 0.5."""
        return PRECISION_HALVES

    @property
    def current_temperature(self):
        """Can not report temperature, so return target_temperature."""
        return self._current_temperature

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._thermostat.target_temperature

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        temperature = round(temperature * 2) / 2  # increments of 0.5
        temperature = min(temperature, self.max_temp)
        temperature = max(temperature, self.min_temp)
        self._is_setting_temperature = True
        self._current_temperature = temperature
        # show current temp now
        self.async_schedule_update_ha_state(force_refresh=False)
        await self.async_set_temperature_now()

    async def async_set_temperature_now(self):
        await self._thermostat.async_set_target_temperature(self._current_temperature)
        self._is_setting_temperature = False
        self._skip_next_update = True

    @property
    def hvac_mode(self):
        """Return the current operation mode."""
        if self._thermostat.mode < 0:
            return HVAC_MODE_OFF
        return EQ_TO_HA_HVAC[self._thermostat.mode]

    @property
    def hvac_modes(self):
        """Return the list of available operation modes."""
        return list(HA_TO_EQ_HVAC)

    async def async_set_hvac_mode(self, hvac_mode):
        """Set operation mode."""
        if hvac_mode == HVACMode.OFF:
            self._current_temperature = EQ3BT_OFF_TEMP
            self._is_setting_temperature = True
        else:
            self._current_temperature = self.target_temperature
            self._is_setting_temperature = False
        self.async_schedule_update_ha_state(force_refresh=False)

        await self._thermostat.async_set_mode(HA_TO_EQ_HVAC[hvac_mode])
        self._skip_next_update = True

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        return EQ3BT_OFF_TEMP

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        return EQ3BT_MAX_TEMP

    @property
    def extra_state_attributes(self):
        """Return the device specific state attributes."""
        schedule = {}

        def stringifyTime(timeObj):
            if isinstance(timeObj, time):
                return str(timeObj.hour) + ":" + str(timeObj.minute)
            return None

        for day in self._thermostat.schedule:
            obj = self._thermostat.schedule[day]

            def mapFunc(hourObj):
                return {
                    "target_temp": hourObj.target_temp,
                    "next_change_at": stringifyTime(hourObj.next_change_at),
                }

            schedule[day] = {
                "base_temp": obj.base_temp,
                "next_change_at": stringifyTime(obj.next_change_at),
                "hours": list(map(mapFunc, obj.hours)),
            }
        dev_specific = {
            ATTR_STATE_AWAY_END: self._thermostat.away_end,
            ATTR_STATE_LOCKED: self._thermostat.locked,
            ATTR_STATE_LOW_BAT: self._thermostat.low_battery,
            ATTR_STATE_VALVE: self._thermostat.valve_state,
            ATTR_STATE_WINDOW_OPEN: self._thermostat.window_open,
            "rssi": self._thermostat.rssi,
            "firmware_version": self._thermostat.firmware_version,
            "device_serial": self._thermostat.device_serial,
            "schedule": schedule,
        }

        return dev_specific

    async def fetch_serial(self):
        await self._thermostat.async_query_id()
        self.async_schedule_update_ha_state(force_refresh=True)
        _LOGGER.debug("[%s] serial: %s", self._name, self._thermostat.device_serial)

    async def fetch_schedule(self):
        _LOGGER.debug("[%s] fetch_schedule", self._name)
        for x in range(0, 7):
            await self._thermostat.async_query_schedule(x)
        self.async_schedule_update_ha_state(force_refresh=True)
        _LOGGER.debug(
            "[%s] schedule (day %s): %s", self._name, self._thermostat.schedule
        )

    def set_schedule(self, day: int = 0):
        _LOGGER.debug("[%s] set_schedule (day %s)", self._name, day)

    @property
    def preset_mode(self):
        """Return the current preset mode, e.g., home, away, temp.
        Requires SUPPORT_PRESET_MODE.
        """
        return EQ_TO_HA_PRESET.get(self._thermostat.mode)

    @property
    def preset_modes(self):
        """Return a list of available preset modes.
        Requires SUPPORT_PRESET_MODE.
        """
        return list(HA_TO_EQ_PRESET)

    @property
    def unique_id(self) -> str:
        """Return the MAC address of the thermostat."""
        return format_mac(self._mac)

    async def async_set_preset_mode(self, preset_mode):
        """Set new preset mode."""
        if preset_mode == PRESET_FORCE_UPDATE:
            self.async_schedule_update_ha_state(force_refresh=True)
            return
        if preset_mode == PRESET_OPEN:
            self._current_temperature = EQ3BT_MAX_TEMP
            self._is_setting_temperature = True
            self.async_schedule_update_ha_state(force_refresh=False)
        if preset_mode == PRESET_CLOSED:
            self._current_temperature = EQ3BT_OFF_TEMP
            self._is_setting_temperature = True
            self.async_schedule_update_ha_state(force_refresh=False)
        if preset_mode == PRESET_NONE:
            await self.async_set_hvac_mode(HVACMode.HEAT)
        await self._thermostat.async_set_mode(HA_TO_EQ_PRESET[preset_mode])

        # by now, the target temperature should have been (maybe set) and fetched
        self._current_temperature = self.target_temperature
        self._is_setting_temperature = False
        self._skip_next_update = True

    async def async_update(self):
        """Update the data from the thermostat."""
        if self._skip_next_update:
            self._skip_next_update = False
            _LOGGER.debug("[%s] skipped update", self.name)
        else:
            try:
                await self._thermostat.async_update()
                if self._is_first_update:
                    await self.fetch_serial()
                    await self.fetch_schedule()
                    self._is_first_update = False

                if self._is_setting_temperature:
                    await self.async_set_temperature_now()
                else:
                    # temperature may have been updated from the thermostat
                    self._current_temperature = self._thermostat.target_temperature
            except Exception as ex:
                # otherwise, if this happens during the first update, the entity will be dropped and never update
                _LOGGER.error(
                    "[%s] Error updating, will retry later: %s", self._name, ex
                )
