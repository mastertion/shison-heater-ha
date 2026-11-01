#pragma once
// 提升 ESP32 蓝牙发射功率到 +9dBm（默认 0dBm）
// 目的：让 CONNECT_IND 能抵达远处的中科蓝讯(Bluetrum)外设 SICN 加热器
#include "esp_bt.h"

inline void sicn_boost_tx_power() {
  // DEFAULT / 广播 / 扫描 三类全部拉满
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_DEFAULT, ESP_PWR_LVL_P9);
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_ADV, ESP_PWR_LVL_P9);
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_SCAN, ESP_PWR_LVL_P9);
  // 连接句柄 0-8 全部拉满
  for (int i = 0; i <= 8; i++) {
    esp_ble_tx_power_set((esp_ble_power_type_t)(ESP_BLE_PWR_TYPE_CONN_HDL0 + i), ESP_PWR_LVL_P9);
  }
}
