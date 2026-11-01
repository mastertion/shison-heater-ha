"""binary_sensor 平台：BLE 链路连接状态。"""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    mgr = hass.data[DOMAIN][entry.entry_id]
    ent = SicnConnectionBinarySensor(mgr)
    async_add_entities([ent])
    mgr.add_listener(ent.async_write_ha_state)


class SicnConnectionBinarySensor(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_name = "连接状态"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, mgr):
        self._mgr = mgr
        self._attr_unique_id = f"{mgr.address}_connection"
        self._attr_device_info = mgr.device_info

    @property
    def is_on(self):
        return self._mgr.data.get("connected", False)
