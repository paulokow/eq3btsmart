"""
Bleak connection backend.
This creates a new event loop that is used to integrate bleak's
asyncio functions to synchronous architecture of python-eq3bt.
"""
import asyncio
import logging
import asyncio

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from homeassistant.components.esphome.bluetooth.characteristic import (
    BleakGATTCharacteristic,
)
from bleak_retry_connector import establish_connection
from homeassistant.core import HomeAssistant
from homeassistant.components import bluetooth

from . import BackendException

REQUEST_TIMEOUT = 1
RETRY_BACK_OFF = 1
RETRIES = 10
RECONNECT_AFTER_RETRIES = 5

# Handles in linux and BTProxy are off by 1. Using UUIDs instead for consistency
PROP_WRITE_UUID = "3fa4585a-ce4a-3bad-db4b-b8df8179ea09"
PROP_NTFY_UUID = "d0e8434d-cd29-0996-af41-6c90f4e0eb2a"

# bleak backends are very loud on debug, this reduces the log spam when using --debug
# logging.getLogger("bleak.backends").setLevel(logging.WARNING)

_LOGGER = logging.getLogger(__name__)


class BleakConnection:
    """Representation of a BTLE Connection."""

    def __init__(
        self,
        mac: str,
        name: str,
        hass: HomeAssistant,
        callback,
    ):
        """Initialize the connection."""
        self._mac = mac
        self._name = name
        self._hass = hass
        self._callback = callback
        self._notifyevent = asyncio.Event()
        self.rssi = None
        self._lock = asyncio.Lock()

    def _on_disconnected(self, client: BleakClient) -> None:
        _LOGGER.debug("%s: Disconnected from device; rssi: %s", self._name, self.rssi)

    async def async_get_connection(self):
        ble_device = bluetooth.async_ble_device_from_address(
            self._hass, self._mac, connectable=True
        )
        if not ble_device:
            raise BackendException("Can't find device")
        self.rssi = ble_device.rssi
        _LOGGER.debug(
            "[%s] Connecting with ble_device, rssi: %s",
            self._name,
            ble_device.rssi,
        )
        conn = await establish_connection(
            BleakClient,
            ble_device,
            self._name,
            self._on_disconnected,
            # MAX_ATTEMPTS=2,  # there is a retry loop on make_request
        )
        if conn.is_connected:
            _LOGGER.debug("[%s] Connected", self._name)
            try:
                # TODO: verify that this
                paired = await conn.pair(
                    1  # 1 = pairing with no protection https://bleak.readthedocs.io/en/latest/backends/windows.html?highlight=pair#bleak.backends.winrt.client.BleakClientWinRT.pair
                )
                _LOGGER.debug("[%s] Paired: %s ", self._name, paired)
            except Exception as ex:
                _LOGGER.warn("[%s] Failed paring: %s ", self._name, ex)
        else:
            raise BackendException("Can't connect")
        return conn

    async def on_notification(self, handle: BleakGATTCharacteristic, data: bytearray):
        """Handle Callback from a Bluetooth (GATT) request."""
        if PROP_NTFY_UUID == handle.uuid:
            self._notifyevent.set()
            self._callback(data)
        else:
            _LOGGER.error(
                "[%s] wrong charasteristic: %s, %s",
                self._name,
                handle.handle,
                handle.uuid,
            )

    async def async_make_request(self, value, retries=RETRIES):
        """Write a GATT Command without callback - not utf-8."""
        async with self._lock:  # only one concurrent request per thermostat
            i = 0
            conn = None
            while True:
                i += 1
                try:
                    if i == RECONNECT_AFTER_RETRIES and conn and conn.is_connected:
                        await conn.disconnect()
                    conn = await self.async_get_connection()
                    self._notifyevent.clear()
                    await conn.start_notify(PROP_NTFY_UUID, self.on_notification)
                    await conn.write_gatt_char(PROP_WRITE_UUID, value)
                    await asyncio.wait_for(self._notifyevent.wait(), REQUEST_TIMEOUT)
                    await conn.stop_notify(PROP_NTFY_UUID)
                    return
                    # keep connection alive
                    # await conn.disconnect()
                except Exception as ex:
                    _LOGGER.warning(
                        "[%s] Broken connection [retry %s/%s]: %s",
                        self._name,
                        i,
                        retries,
                        ex,
                    )
                    if i >= retries:
                        raise ex
                    await asyncio.sleep(RETRY_BACK_OFF)
