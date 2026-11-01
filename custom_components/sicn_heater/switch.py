"""switch 平台：切换设备显示单位 (℃ <-> ℉)。

设备指令 31 02 是"切换"而非"设定", 因此开关以当前单位为准做幂等切换：
开 = 设备显示为 ℉, 关 = 设备显示为 ℃。

注意：31 02 是 toggle 语义，底层写入必须"只发一次"。早期为了兜底带响应
写失败会重发无响应写，导致切过去又切回来，表现成「点按钮没反应」。
"""

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    mgr = hass.data[DOMAIN][entry.entry_id]
    ent = SicnUnitSwitch(mgr)
    async_add_entities([ent])
    mgr.add_listener(ent.async_write_ha_state)


class SicnUnitSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "华氏单位"

    def __init__(self, mgr):
        self._mgr = mgr
        self._attr_unique_id = f"{mgr.address}_unit"
        self._attr_device_info = mgr.device_info

    @property
    def available(self):
        return self._mgr.available

    @property
    def is_on(self):
        return self._mgr.data.get("unit") == "F"

    async def _toggle_to(self, want: str):
        try:
            await self._mgr.async_toggle_unit()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("切换单位到 %s 失败: %s", want, err)
            raise HomeAssistantError(f"切换温度单位失败: {err}") from err

    async def async_turn_on(self, **kwargs):
        if self._mgr.data.get("unit") != "F":
            await self._toggle_to("F")

    async def async_turn_off(self, **kwargs):
        if self._mgr.data.get("unit") != "C":
            await self._toggle_to("C")
