"""常量定义：SICN 加热器 BLE 协议 (逆向实证, 与鸿蒙 App v26.46 一致)。"""

DOMAIN = "sicn_heater"

# GATT
SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"

# 写指令 (全部写到 FFF1)
CMD_SYNC = bytes([0x01, 0x03])        # 握手/刷新状态
CMD_TARGET = bytes([0x31, 0x01, 0, 0])  # 设目标温度模板
CMD_UNIT = bytes([0x31, 0x02])       # 切换温度单位 ℃<->℉
CMD_HOUR_HISTORY = bytes([0x31, 0x04])  # 请求小时历史
CMD_MINUTE_HISTORY = bytes([0x31, 0x03])  # 请求分钟历史

# config flow 字段
CONF_ADDRESS = "address"

# 设备名前缀 (用于蓝牙发现匹配, 实际匹配靠 service_uuid, 这里仅作展示)
DEFAULT_NAME = "SICN 加热器"

# 目标温度范围 (℃, HA 内统一以摄氏度呈现)
MIN_TEMP = 18.0
MAX_TEMP = 34.0
STEP = 0.5

# 模式映射 (v26.46 修正后的权威映射)
MODE_TEXT = {
    0: "降温",
    1: "恒温",
    2: "加热",
    3: "超温",
    4: "离水断电",
    5: "探头故障",
}
