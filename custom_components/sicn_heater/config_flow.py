"""config_flow：支持蓝牙自动发现 + 手动输入 MAC。"""

import re
import voluptuous as vol

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigEntry, OptionsFlow
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_ADDRESS, DOMAIN

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


class SicnConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        errors = {}
        if user_input is not None:
            addr = user_input[CONF_ADDRESS].strip().upper()
            if not _MAC_RE.match(addr):
                errors["base"] = "invalid_address"
            else:
                await self.async_set_unique_id(addr)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="SICN 加热器", data={CONF_ADDRESS: addr})
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): str}),
            errors=errors,
        )

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> FlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=discovery_info.name or "SICN 加热器",
            data={CONF_ADDRESS: discovery_info.address},
        )

    # 已配置设备的重连/地址更新入口
    async def async_step_reauth(self, entry_data) -> FlowResult:
        return await self.async_step_user()
