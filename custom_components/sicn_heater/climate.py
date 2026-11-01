"""climate 平台：把加热器作为恒温器暴露给 HA。

HA 内统一以摄氏度呈现：HA 前端的 climate 卡片只认全局温度单位
（hass.config.unit_system.temperature），不读实体自身的 temperature_unit，
所以实体层做单位跟随是无效的（反而会造成 ℉ 数值被标成 ℃ 的错误）。
设备侧切到 ℉ 只影响设备屏幕，HA 卡片恒为 ℃，数值由设备原始帧换算而来。
"""

import logging

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature

from .const import DOMAIN, MAX_TEMP, MIN_TEMP, STEP

_LOGGER = logging.getLogger(__name__)

# 模式 -> HA 动作 (加热器只有制热, 其余视为待机/空闲)
HVAC_ACTION_MAP = {
    0: HVACAction.IDLE,  # 降温
    1: HVACAction.IDLE,  # 恒温
    2: HVACAction.HEATING,  # 加热
    3: HVACAction.IDLE,  # 超温
    4: HVACAction.IDLE,  # 离水断电
    5: HVACAction.IDLE,  # 探头故障
}


async def async_setup_entry(hass, entry, async_add_entities):
    mgr = hass.data[DOMAIN][entry.entry_id]
    ent = SicnClimate(mgr)
    async_add_entities([ent])
    mgr.add_listener(ent.async_write_ha_state)


class SicnClimate(ClimateEntity):
    _attr_has_entity_name = True
    _attr_name = "加热器"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_target_temperature_step = STEP
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, mgr):
        self._mgr = mgr
        self._attr_unique_id = f"{mgr.address}_climate"
        self._attr_device_info = mgr.device_info

    @property
    def available(self):
        return self._mgr.available

    @property
    def current_temperature(self):
        return self._mgr.data.get("current")

    @property
    def target_temperature(self):
        return self._mgr.data.get("target")

    @property
    def hvac_action(self):
        mode = self._mgr.data.get("mode")
        return HVAC_ACTION_MAP.get(mode, HVACAction.IDLE)

    @property
    def hvac_mode(self):
        return HVACMode.HEAT

    async def async_set_temperature(self, **kwargs):
        t = kwargs.get(ATTR_TEMPERATURE)
        if t is None:
            return
        t = min(MAX_TEMP, max(MIN_TEMP, float(t)))
        await self._mgr.async_set_target(t)
