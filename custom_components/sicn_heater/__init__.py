"""SICN 加热器 BLE 集成入口。"""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .ble import SicnHeaterBle

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["climate", "sensor", "binary_sensor", "switch", "button"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    address = entry.data["address"]
    name = entry.title or "SICN 加热器"
    mgr = SicnHeaterBle(hass, address, name)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = mgr

    await mgr.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        mgr = hass.data[DOMAIN].pop(entry.entry_id, None)
        if mgr:
            await mgr.async_stop()
    return unload_ok
