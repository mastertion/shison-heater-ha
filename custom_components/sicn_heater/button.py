"""button 平台：主动刷新状态 (写 01 03 握手)。"""

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import EntityCategory
from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    mgr = hass.data[DOMAIN][entry.entry_id]
    ent = SicnRefreshButton(mgr)
    async_add_entities([ent])
    mgr.add_listener(ent.async_write_ha_state)


class SicnRefreshButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "刷新状态"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, mgr):
        self._mgr = mgr
        self._attr_unique_id = f"{mgr.address}_refresh"
        self._attr_device_info = mgr.device_info

    async def async_press(self):
        await self._mgr.async_refresh()
