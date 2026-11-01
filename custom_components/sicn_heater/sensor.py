"""sensor 平台：加热器工作模式 + 当前水温 (只读)。"""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN, MODE_TEXT


async def async_setup_entry(hass, entry, async_add_entities):
    mgr = hass.data[DOMAIN][entry.entry_id]
    ents = [SicnModeSensor(mgr), SicnCurrentTempSensor(mgr)]
    async_add_entities(ents)
    for ent in ents:
        mgr.add_listener(ent.async_write_ha_state)


class SicnModeSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "模式"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, mgr):
        self._mgr = mgr
        self._attr_unique_id = f"{mgr.address}_mode"
        self._attr_device_info = mgr.device_info

    @property
    def available(self):
        return self._mgr.available

    @property
    def native_value(self):
        mode = self._mgr.data.get("mode")
        if mode is None:
            return None
        return MODE_TEXT.get(mode, f"未知({mode})")


class SicnCurrentTempSensor(SensorEntity):
    """当前水温。HA 全局仍以摄氏度计，与 climate 实体保持一致。"""

    _attr_has_entity_name = True
    _attr_name = "当前温度"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(self, mgr):
        self._mgr = mgr
        self._attr_unique_id = f"{mgr.address}_current_temp"
        self._attr_device_info = mgr.device_info

    @property
    def available(self):
        return self._mgr.available

    @property
    def native_value(self):
        return self._mgr.data.get("current")
