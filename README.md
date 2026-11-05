# SICN 加热器接入 Home Assistant（BLE）

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/mastertion/shison-heater-ha)](https://github.com/mastertion/shison-heater-ha/releases)

让 Home Assistant 接管 **SICN-HEATER-\*** 蓝牙加热器：读当前水温、改目标温度、切设备显示单位，
全部通过 BLE 完成，不依赖原厂手机 App。

```
                  ┌──────────────────────────────────┐
                  │ 自定义集成 custom_components/     │
                  │ sicn_heater                      │
 SICN-HEATER-E84  │  climate / sensor / switch /     │
 (BLE 服务 FFF0   │  binary_sensor / button          │
  特征 FFF1)      └──────────────┬───────────────────┘
        ▲                        │
        │ BLE                    ▼
        └──────────── Home Assistant
                    ▲
       ┌────────────┴─────────────┐
       │                          │
  ESP32 蓝牙代理            本机 USB 蓝牙适配器
  (ESPHome bluetooth_proxy)  (hci0，需直通给 HA 虚拟机)
```

两种接入方式都支持，集成代码完全一样 —— HA 的 Bluetooth 集成会把 ESP32 代理
和本机适配器统一纳为「蓝牙来源」，集成只按地址/广播找设备。

> ⚠️ **加热器是单连接设备**：HA 连着的时候，手机 App 连不上，反过来也一样。
> 真机实测，最常见「连不上」的原因就是手机 App 还在后台占着连接。

---

## 目录结构

```
ha/
├── hacs.json                        # HACS 自定义仓库清单（决定「一键安装」能不能用）
├── brand/icon.png                   # HACS / HA 显示的集成图标
├── custom_components/sicn_heater/   # HA 自定义集成
│   ├── manifest.json  __init__.py  config_flow.py  const.py
│   ├── ble.py                       # BLE 连接 / 协议解析（核心）
│   ├── climate.py  sensor.py  switch.py  binary_sensor.py  button.py
│   └── translations/en.json
├── esp32_bt_proxy.yaml              # ESP32 蓝牙代理的 ESPHome 配置
├── txpower.h                        # 提升 ESP32 BLE 发射功率（可选）
├── secrets.yaml.example             # 复制成 secrets.yaml 填你自己的 WiFi
└── deploy.sh                        # 在 HA 主机上部署并重启
```

---

## 1. 安装集成

### 方式一：HACS 一键安装（推荐）

1. 打开 **HACS** → 右上角 `⋮` → **自定义存储库 / Custom repositories**
2. 仓库地址填 `https://github.com/mastertion/shison-heater-ha`
   （**末尾不要带 `.git`**，HACS 会把它当成仓库名的一部分而报「找不到仓库」）
3. 类别选 **集成 / Integration** → 点「添加」
4. 回到 HACS → **集成** → 搜索 `SICN` → 下载
5. **重启 Home Assistant**（不是重载集成，必须重启）

> 前提是 HA 里**已经装好 HACS**。如果 HACS 侧栏里根本没有 HACS 这一项，先按
> [官方安装说明](https://hacs.xyz/docs/setup/download/) 装好它，再回来做第 1 步。
>
> 本仓库符合 HACS 自定义集成规范：根目录有 `hacs.json` 与 `brand/icon.png`，
> `custom_components/` 下只有一个集成目录，`manifest.json` 带齐
> `domain` / `documentation` / `issue_tracker` / `codeowners` / `name` / `version`。
> GitHub Release 不是硬性要求（没有时 HACS 用默认分支），本仓库仍然每个版本都会发，
> 方便在下载页里选择版本。

> **维护者提醒**：HACS 显示的版本来自 GitHub Release，不是 commit。改完代码要发新版：
> 改 `manifest.json` 的 `version` → commit → 打 tag（如 `v1.0.1`）→ 建 GitHub Release。

### 方式二：手动拷贝

把 `custom_components/sicn_heater/` 整个目录放到 HA 配置目录下：

```
/config/custom_components/sicn_heater/
```

HAOS / 插件版可以用 **Samba Share**、**Terminal & SSH** 或 File Editor 传进去，然后：

```sh
ha core restart
```

### 添加设备

重启后：**设置 → 设备与服务 → 添加集成 → 搜「SICN 加热器」**，输入加热器 MAC 地址；
若加热器正在广播，HA 也会直接弹「已发现」通知。

> **手动改 py 文件后务必删掉 `__pycache__` 再重启**。只重启而不清缓存时，新增的实体不会出现，
> 而且日志里没有任何报错 —— 这个坑排查起来非常费时间。（HACS 安装不受影响，它会整包替换。）

---

## 2. 接入方式 A：ESP32 蓝牙代理（推荐）

HA 跑在虚拟机里没有蓝牙、或者蓝牙适配器离加热器太远时，用一块 ESP32 做代理最省事。

### 2.1 烧录

1. 复制 `secrets.yaml.example` 为 `secrets.yaml`，填 WiFi 名称与密码。
2. 把 `esp32_bt_proxy.yaml` 按你的芯片改 `board`（默认 `esp32dev`）。
3. 用 ESPHome 编译烧录，或 OTA 更新。

配置里有三个**必须保留**的关键项，都是真机踩出来的：

| 配置 | 为什么 |
|---|---|
| `bluetooth_proxy: active: true` | 必须为 true，HA 才能通过代理**主动连接**并读写 GATT，而不只是被动扫描 |
| `cache_services: false` | ESP32 默认把 GATT 服务表缓存在 NVS 里，旧缓存**不含描述符**，导致通知永远开不起来（`Invalid handle`）。关掉缓存后每次连接都重新做完整服务发现 |
| `wifi: power_save_mode: none` | ESP32 经典款的 WiFi 省电模式会与 BLE 共存冲突，挤掉连接时隙，表现为 `0x3e failed to establish` |

若你的板子是 pico-d4 之类供电偏弱的模组，还要保留 `CONFIG_ESP_BROWNOUT_DET: "n"`，
否则 BLE+WiFi 同时初始化时的电流峰值会触发欠压复位，OTA「成功」后 bootloader 悄悄回滚旧固件。

`txpower.h` 会把 BLE 发射功率拉到最大，信号边缘场景能明显减少断连。

### 2.2 在 HA 里确认

重启 ESP32 后，**设置 → 设备与服务 → ESPHome** 里能看到该设备，
并且 **Bluetooth** 集成下会多出一个蓝牙来源（通常名为 `<设备名> (MAC)`）。

---

## 3. 接入方式 B：本机 USB 蓝牙适配器

HA 是虚拟机时，蓝牙适配器必须**直通**给虚拟机 —— 插在宿主机上但没直通，
HA 里就是「找不到适配器」。

Unraid 用 `virsh` 热直通（断电重生后依然生效）：

```sh
# 宿主机上先找 dongle 的厂商:设备号
lsusb | grep -i bluetooth          # 例如 0a12:0001（CSR8510）

# 写一份 hostdev 片段，例如 /tmp/bt.xml
# <hostdev mode='subsystem' type='usb'>
#   <source><vendor id='0x0a12'/><product id='0x0001'/></source>
# </hostdev>

virsh attach-device <HA虚拟机名> /tmp/bt.xml --live --config
```

直通成功后 HA 里验证：

```sh
bluetoothctl list      # 应列出一个控制器
bluetoothctl show      # Powered: yes
```

然后 **设置 → 设备与服务 → 添加集成 → Bluetooth**。

> 适配器「用着用着就消失」几乎都是直通丢了（虚拟机重启、宿主 USB 重新枚举），
> 重新 `virsh attach-device` 即可，不用换硬件。

---

## 4. 实体清单

设备添加成功后，会产生这些实体：

| 实体 | 类型 | 说明 |
|---|---|---|
| 加热器 | `climate` | 当前温度 / 目标温度（可调）/ 运行状态（加热中 → 制热，其余 → 空闲） |
| 当前温度 | `sensor` | 纯温度传感器，可画历史曲线、做统计 |
| 模式 | `sensor` | 降温 / 恒温 / 加热 / 超温 / 离水断电 / 探头故障 |
| 连接状态 | `binary_sensor` | BLE 链路是否在线 |
| 华氏单位 | `switch` | 切换**设备屏幕**的显示单位（开 = ℉，关 = ℃） |
| 刷新状态 | `button` | 主动发 `01 03` 握手，让设备立即推一帧状态 |

关于温度单位：**HA 的 climate 卡片只认全局单位系统**（`hass.config.unit_system.temperature`），
不读实体自己声明的单位。所以「华氏单位」开关只影响**设备屏幕**，HA 内部恒以摄氏度存储和展示。

---

## 5. 协议速查

BLE 服务 `0000fff0`，特征 `0000fff1`（notify + read + write）。
**全部数值无偏移**，写入值 = 温度 × 10，小端。

| 命令 | 帧 | 说明 |
|---|---|---|
| 握手 / 同步 | `01 03` | 请求设备回报当前状态 |
| 设目标温度 | `31 01 LO HI` | 值 = 温度×10，小端 |
| 切换显示单位 | `31 02` | **toggle 语义**，发一次切一次 |
| 分历史 | `31 03` | 逐小时采样 |
| 时历史 | `31 04` | 逐分钟采样 |

设备主动推送的帧：

| 帧 | 长度 | 含义 |
|---|---|---|
| `30 01 MODE UNIT T_LO T_HI` | 6 字节 | 状态帧：模式 + 单位 + 当前温度（无扩展字段） |
| `31 01 LO HI` | 4 字节 | 目标温度回显 |
| `01 03` | 2 字节 | 命令回显（无数据） |

工作模式 `MODE` 与 `byte2` 的对应关系（**这是原厂安卓 App 的真实顺序，网上流传的版本是倒的**）：

| 值 | 模式 |
|---|---|
| 0 | 降温 Cooling |
| 1 | 恒温 KeepTemperature |
| 2 | 加热 Heating |
| 3 | 超温 OverTemperature |
| 4 | 离水断电 LeavingWaterPowerOff |
| 5 | 探头故障 ProbeFailure |

数值越大故障越重，3/4/5 视为报警。

当前温度换算：`(byte5 × 256 + byte4) / 10`；若 `UNIT = 1`（华氏），再按 `(t − 32) / 1.8` 转摄氏。

---

## 6. 实现要点（都是真机踩出来的）

### 6.1 CCCD 句柄必须遍历，且要包含 offset = 0

这个设备**不暴露标准 CCCD 描述符**，写 `0100` 开启通知时必须猜句柄。实测：

```
FFF1 值句柄 = 8，CCCD 落在 9（= handle + 0），但下次连接可能变成 handle + 1
```

偏移列表是 `(0, 1, -1, 2, 3, 4, 5, 6)`，**不能只留 0** —— 换一次连接句柄就会漂移，
历史上出现过 `h+0` 报 `Notify acquired`（残留订阅占用）、`h+1` 才成功的案例。

早期只试 `(-1, 1, 2, 3, 4, 5)`，一圈试遍独独跳过唯一正确的 0，
导致「CCCD 全部失败」→ 退化成轮询写响应 → 数据永远不动。

### 6.2 写响应基本恒为空，不能当数据源

`write_gatt_char(response=True)` 绝大多数时候返回 `None`。
早期据此判定「订阅失败」并退化成「等写响应」，结果什么都等不到，
只能靠每 3–4 分钟断线重连碰运气。

正确做法：**订阅 FFF1 通知**，设备会持续主动推 `30 01` 状态帧（实测约每秒一帧）。

### 6.3 所有 GATT 操作必须设超时

`start_notify()` 曾经是唯一没有超时保护的调用。ESP32 代理重启的瞬间，
代理端的 `bluetooth_gatt_start_notify` 请求发出去却没有响应，
这个 `await` 就永久挂死 —— 整个重连协程停摆，**既不重连、也不刷新、也不报错**，
表现为实体莫名其妙变成不可用。

现在的超时：`start_notify` 8s / `stop_notify` 5s / 读写 10s / 服务发现 15s，
订阅阶段另有 45s 总闸。

### 6.4 toggle 类命令严禁重发

`_raw_write()` 早期有个「带响应写失败就改无响应重发一次」的兜底，对幂等命令（设温、握手）无害，
但对 `31 02`（切换单位）是致命的：

```
第 1 次发 → 切到 ℉
第 2 次发 → 又切回 ℃      ← 兜底重发干的
```

结果是开关点了毫无反应，日志里连痕迹都没有（两次写都「成功」了）。
现在 toggle 类命令走单独的 `_raw_write_once()`，只写一次、绝无 fallback。

**结论**：凡是 toggle / 递增 / 单次动作类写命令，底层写入必须严格一次；
只有幂等的 set 类命令才允许「失败重发」。

### 6.5 切换单位会重置目标温度，要主动写回

设备每次收到 `31 02` 都会把目标温度改掉（实测 26.0℃ 切到 ℉ 后自己推了个 28.3℃）。
所以 `async_toggle_unit()` 是「切 → 等新单位回报 → 按新单位换算写回」：

```
切换前记下 cur_c → 发一次 31 02 → 等 data["unit"] 变化（实测 40ms）
→ 按新单位换算 + 步进量化 → 写 31 01 盖回设备的重置值
```

### 6.6 步进按档位分开

| 档位 | 步进 | 量化方式 |
|---|---|---|
| 摄氏度 | 0.5° | `round(c × 2) / 2` |
| 华氏度 | 1° | `round(c × 1.8 + 32)` |

设温也走同一套量化，否则在华氏档会下发 83.3℉ 这种非整数。

℃ → ℉ → ℃ 往返有约 0.1℃ 的偏差（26.0℃ = 78.8℉，按 1℉ 步进取整成 79℉ = 26.11℃），
属步进规则的固有损失，不是算错。

### 6.7 HA 会把 `bleak.BleakClient` 换成包装类

`homeassistant.components.bluetooth` 在 setup 时把 `bleak.BleakClient` 替换为
`habluetooth.HaBleakClientWrapper`，其 `connect()` 会**按地址从所有 scanner 里挑后端** ——
包括 ESP32 代理。这在多来源场景下可能连到你不想要的那条路上去。

需要精确控制走本机 BlueZ 时，得从 `BleakClient.__mro__` 取回原始类再实例化。

---

## 7. 排错

| 现象 | 处理 |
|---|---|
| 添加集成时搜不到 Bluetooth | 适配器没直通给虚拟机，或被宿主/其他 VM 占用（一个 USB 设备只能给一个 VM） |
| 连接状态一直 off | 手机 App 正在连（单连接设备）→ 关掉手机蓝牙；或距离太远 |
| 连上了但数据不动 | CCCD 没开成功 → 看日志有没有「订阅成功」，没有就换句柄；或代理 `cache_services` 没关 |
| 日志停在「连接成功 + GATT 发现」后无输出 | `start_notify` 挂死，检查是否所有 GATT 调用都有超时 |
| 点「华氏单位」没反应 | 检查 `31 02` 是否被发了两次（toggle 抵消） |
| 目标温度显示 unknown | 设备只在设温时推 `31 01`；在 HA 里调一次温度即可拿到 |
| 实体不可用但蓝牙正常 | 看 HA 日志有没有 D-Bus 报错；必要时把手机 App 彻底杀掉再重连 |
| 地址变了（`41:xx` 开头） | 那是可解析私有地址（RPA），设备名不变就没问题，按广播重新发现即可 |

---

## 8. 已知限制

- **不能远程关机**：协议里没有开关机命令，只有握手、设温、单位、历史四类。
  设备物理上只能靠拔电源关机。所以 climate 实体**不提供** `turn_on` / `turn_off`，
  仪表盘上也不要放电源按钮（Mushroom 之类的卡片会自带一个，点了必报「不支持动作 climate.turn_off」）。
- **单连接**：HA 与手机 App 不能同时连。
- **目标温度只能被动获取**：`31 01` 是设备在设温时主动推的，不主动轮询；断连重连后会重新拿到一次。
- 历史曲线（`31 03` / `31 04`）尚未暴露为实体。

---

## 9. 部署辅助

在 HA 主机上执行（先把本目录传到 `/tmp/sicn_ha`）：

```sh
sh /tmp/sicn_ha/deploy.sh
```

脚本会把 `custom_components/sicn_heater` 覆盖到 `/config/` 并 `ha core restart`。
