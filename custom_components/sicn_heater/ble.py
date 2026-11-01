"""SICN 加热器 BLE 管理器。

负责：发现后的连接维持、FFF1 通知使能、握手 01 03、状态解析、
设温/切单位/刷新写指令、断线自动重连。
温度在 HA 内统一以摄氏度呈现：设备当前单位若为 ℉ 则换算成 ℃。

协议要点（与鸿蒙 App v26.46 一致，逆向实证）：
- FFF1 同时带 notify/read/write 属性，但部分固件/代理下没有 CCCD，无法订阅，
  只能靠写命令的响应 + 读轮询拿状态。
- 写推荐用 response=True 并解析返回字节；读轮询作为兜底。
"""

import asyncio
import logging
import time

import bleak
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection


def _real_client_class():
    """取「未被 HA 替换」的原始 bleak 公共客户端类。

    HA 在 bluetooth 集成 async_setup 里把 bleak.BleakClient 换成了
    habluetooth.HaBleakClientWrapper —— 它连接时会按地址重新从所有扫描器
    里挑后端，一旦广播缓存过期就报
    "No backend with an available connection slot that can reach address ..."。
    加热器广播很稀疏（几分钟一次），必然踩这个坑，所以取回原始类直接连。
    """
    cls = bleak.BleakClient
    if "habluetooth" in getattr(cls, "__module__", ""):
        for base in getattr(cls, "__mro__", [])[1:]:
            if getattr(base, "__module__", "") == "bleak":
                return base
    return cls

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    async_ble_device_from_address,
    async_register_callback,
)
from homeassistant.components.bluetooth.match import BluetoothCallbackMatcher
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    CHAR_UUID,
    CMD_SYNC,
    CMD_TARGET,
    CMD_UNIT,
    DOMAIN,
    MODE_TEXT,
)

_LOGGER = logging.getLogger(__name__)

CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"

# GATT 操作超时（秒）。**每一个** start_notify / stop_notify / 读写都必须套
# wait_for：ESPHome 代理重启或掉线时，bluetooth_gatt_* 请求会发出去但永远
# 没有响应，await 就此永久挂死 —— 表现为「连接成功」之后再无任何日志，
# 既不重连也不刷新（20:31 拔插 ESP32 后实测踩过）。有超时才能自愈。
GATT_TIMEOUT = 8.0

# 只用本机 USB 蓝牙适配器(hci0)。不依赖任何 ESPHome/ESP32 蓝牙代理：
# BlueZ 后端发现的设备对象 details 里带 D-Bus "path"，默认路由即走本地适配器；
# 代理发现的设备对象没有 path（是 EsphomeBLEDevice），一律忽略。



class SicnHeaterBle:
    """单个加热器的 BLE 连接与状态管理。"""

    def __init__(self, hass, address: str, name: str):
        self.hass = hass
        self.address = address
        self.name = name
        self.client = None
        self.connected = False
        self._listeners = []
        self._reconnect_task = None
        self._conn_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._notify_char = None  # 实际带 notify 且含 CCCD 的特征
        self._write_char = None   # 实际带 write 属性的特征
        self._read_char = None    # 实际带 read 属性的特征(无通知时轮询用)
        # 广播事件: 收到目标地址的广播即刻置位, 触发立即连接
        self._adv_event = asyncio.Event()
        self._cancel_adv_cb = None
        # 最近一条广播对象（自带真实 D-Bus path），连接时优先使用
        self._last_adv = None
        self._last_adv_time = 0.0
        # 来自 ESPHome 代理的那条广播，单独留一份并优先使用。
        # 原因：本机 hci0 连 RPA 设备（41:xx 前缀）后 GATT 订阅必失败
        # （BlueZ 报 StartNotify 方法不存在），只有代理路径订阅得通。
        self._last_proxy_adv = None
        self._last_proxy_adv_time = 0.0
        # 最近一次成功解析状态帧的时间（用于判断是否需要重连刷新）
        self._last_status = 0.0
        # 通知到达事件: 订阅后用于验证通知真的在流通(伪 CCCD 句柄校验)
        self._notify_event = asyncio.Event()
        # 统一以摄氏度保存, 避免单位切换造成混乱
        self.data = {
            "current": None,   # 当前温度 ℃
            "target": None,    # 目标温度 ℃
            "mode": None,      # 模式码
            "unit": "C",       # 设备当前显示单位 C/F
            "connected": False,
        }

    @property
    def available(self) -> bool:
        """连接着、或最近 10 分钟拿到过状态，就算可用（避免重连间隙实体闪 unavailable）。"""
        if self.connected:
            return True
        return bool(self._last_status) and (time.monotonic() - self._last_status) < 600

    # ---------------------------------------------------------------- 设备信息
    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.address)},
            name=self.name,
            manufacturer="SHISON",
            model="SICN-HEATER",
        )

    # ---------------------------------------------------------------- 监听
    def add_listener(self, cb):
        if cb not in self._listeners:
            self._listeners.append(cb)

    def _notify(self):
        for cb in self._listeners:
            try:
                cb()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 生命周期
    def _on_adv(self, service_info, *_args):
        """收到目标地址广播 → 唤醒重连循环(毫秒级响应短窗口广播)。"""
        now = time.monotonic()
        # 保存这条广播本身：它自带真实 D-Bus path 与 rssi，
        # 是连接时最可靠的设备对象（HA 的广播缓存几分钟就过期）
        self._last_adv = service_info
        self._last_adv_time = now
        # 单独记录代理来源（EsphomeBLEDevice 没有 D-Bus path）：
        # 代理连接的 GATT 订阅可靠，本机 hci0 对 RPA 设备订阅必失败
        dev = getattr(service_info, "device", service_info)
        if "path" not in (getattr(dev, "details", None) or {}):
            self._last_proxy_adv = service_info
            self._last_proxy_adv_time = now
        if now - getattr(self, "_last_rssi_log", 0.0) > 30:
            self._last_rssi_log = now
            _LOGGER.debug(
                "SICN 广播 RSSI=%s dBm (来源 %s, connectable=%s)",
                service_info.rssi,
                service_info.source,
                getattr(service_info, "connectable", "?"),
            )
        self._adv_event.set()

    async def async_start(self):
        self._cancel_adv_cb = async_register_callback(
            self.hass,
            self._on_adv,
            BluetoothCallbackMatcher(address=self.address),
            BluetoothScanningMode.ACTIVE,
        )
        self._reconnect_task = self.hass.loop.create_task(self._reconnect_loop())
        self._notify()

    async def async_stop(self):
        if self._cancel_adv_cb:
            try:
                self._cancel_adv_cb()  # 注册返回值即取消回调
            except Exception:  # noqa: BLE001
                pass
            self._cancel_adv_cb = None
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        await self._disconnect()

    async def _reconnect_loop(self):
        while True:
            # 等待广播(最多8s兜底轮询一次); 广播一到立即出手, 不错过短窗口
            try:
                await asyncio.wait_for(self._adv_event.wait(), timeout=8)
                self._adv_event.clear()
            except asyncio.TimeoutError:
                pass
            try:
                await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("SICN 连接异常: %s", err)
                # 失败后退避，但广播一到立刻重试（加热器广播很稀疏，不能干等）
                try:
                    await asyncio.wait_for(self._adv_event.wait(), timeout=25)
                except asyncio.TimeoutError:
                    pass
                self._adv_event.clear()

    def _pick_device(self):
        """选连接用的设备对象，返回 (device, 是否走 HA 代理路由)。

        - 广播来自本机 BlueZ (details 带 "path") → 用原始客户端直连，绕开 HA 路由
        - 广播来自 ESPHome 代理 (EsphomeBLEDevice，无 path) → 只能用 HA 的
          包装器连接，它会把连接派给那台代理（这正是我们要的）
        加热器广播很稀疏，HA 的 discovered_devices 几十秒就被剪掉，
        所以优先用 _on_adv 保存的那条广播对象本身，而不是按地址回查缓存。
        """
        # ---- 首选：ESPHome 代理来源的广播 ----
        # 本机 hci0 连这台加热器（隐私地址 41:xx/RPA）后，GATT 订阅必定失败：
        # BlueZ 报 "StartNotify ... doesn't exist"，随后退化成等写响应而僵死。
        # 历史上唯一订阅成功过的路径就是 ESP32 代理，所以只要有代理广播就用它。
        pa = getattr(self, "_last_proxy_adv", None)
        pa_age = time.monotonic() - getattr(self, "_last_proxy_adv_time", 0.0)
        if pa is not None and pa_age < 180:
            self._note_pick("ESP32 代理")
            return getattr(pa, "device", pa), True

        last = self._last_adv
        # BlueZ 对未配对设备会很快清掉 D-Bus 对象，超过 60 秒的广播对象再去连
        # 只会得到 "device not found"，所以只认一分钟内的
        if last is not None and (time.monotonic() - self._last_adv_time) < 60:
            # BluetoothServiceInfoBleak 不是 BLEDevice 子类，要取 .device
            dev = getattr(last, "device", last)
            if "path" in (getattr(dev, "details", None) or {}):
                self._note_pick("本机 hci0")
                return dev, False
            self._note_pick("ESP32 代理")
            return dev, True
        cached = async_ble_device_from_address(self.hass, self.address, connectable=True)
        if cached is not None:
            details = cached.details or {}
            if "path" in details:
                self._note_pick("本机 hci0(缓存)")
                return cached, False
            self._note_pick("ESP32 代理(缓存)")
            return cached, True
        path = "/org/bluez/hci0/dev_" + self.address.replace(":", "_").upper()
        if (time.monotonic() - (self._last_adv_time or 0)) > 60:
            _LOGGER.debug("SICN 广播超 60s 未更新，等下一轮广播再连")
            return None, False
        self._note_pick("本机 hci0(路径兜底)")
        return BLEDevice(self.address, self.name or "SICN-HEATER", {"path": path}), False

    def _note_pick(self, how: str):
        """连接路径变化时打一条 info，方便一眼看出走的是代理还是本机。"""
        if getattr(self, "_pick_path", None) != how:
            self._pick_path = how
            _LOGGER.info("SICN 连接路径: %s", how)

    async def _connect(self):
        async with self._conn_lock:
            if self.connected:
                return
            # 只有「最近真的看到广播」才尝试连接：加热器广播极稀疏，
            # 盲连只会让 BlueZ 反复报 device not found，纯属空转。
            fresh = (
                self._last_adv is not None
                and (time.monotonic() - self._last_adv_time) < 180
            )
            if not fresh:
                if time.monotonic() - getattr(self, "_last_skip_log", 0) > 300:
                    self._last_skip_log = time.monotonic()
                    _LOGGER.debug(
                        "SICN 已 %.0f 秒没收到该设备广播，暂不尝试连接（等广播）",
                        time.monotonic() - (self._last_adv_time or 0),
                    )
                return
            device, via_proxy = self._pick_device()
            if device is None:
                # hci0 没看到设备：不盲连，等 _on_adv 唤醒下一轮
                return
            _LOGGER.debug(
                "SICN 连接目标: %s (%s)",
                self.address,
                "经 ESPHome 代理" if via_proxy else "本机蓝牙适配器",
            )
            if via_proxy:
                # 只能走 HA 的包装器：它会按地址把连接派给那台 ESPHome 代理
                client = await establish_connection(
                    bleak.BleakClient, device, self.address, max_attempts=3
                )
            else:
                # 直接用原始 bleak 客户端 connect()，不经过 HA 的地址路由包装器，
                # 也不经过 establish_connection（它会重新查广播缓存）。
                client_cls = _real_client_class()
                client = client_cls(device, timeout=35.0)
                if getattr(self, "_client_cls_logged", False) is False:
                    self._client_cls_logged = True
                    _LOGGER.info(
                        "SICN 使用客户端类: %s.%s",
                        client_cls.__module__,
                        client_cls.__name__,
                    )
                try:
                    await asyncio.wait_for(client.connect(), timeout=30)
                except BaseException:
                    if getattr(client, "disconnect", None) is not None:
                        try:
                            await client.disconnect()
                        except Exception:  # noqa: BLE001
                            pass
                    raise
            self.client = client
            _LOGGER.info("SICN 连接成功: %s", self.address)

            # ---- 服务发现 + 完整描述符 dump（定位真实数据通道） ----
            # 强制刷新 GATT 缓存：代理在被动扫描缓存里常丢 FFF1 的 CCCD，
            # 主动连接后重新发现有可能补出描述符。
            try:
                await asyncio.wait_for(client.get_services(), timeout=15)
                _LOGGER.info("SICN 已强制刷新 GATT 服务缓存")
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("get_services 失败: %s", err)
            notify_candidates = []
            cand_fff0 = []
            cand_other = []
            write_char = None
            read_char = None
            for service in client.services:
                svc_uid = str(service.uuid).lower()
                for ch in service.characteristics:
                    props = set(ch.properties)
                    uid = str(ch.uuid).lower()
                    descs = [str(d.uuid).lower() for d in getattr(ch, "descriptors", [])]
                    _LOGGER.info(
                        "SVC %s CH %s props=%s descs=%s",
                        svc_uid[:8], uid[:8], sorted(props), descs,
                    )
                    has_ccc = CCCD_UUID in descs
                    has_notify = ("notify" in props or "indicate" in props)
                    # FFF1 即使 GATT 表里没有 CCCD 也纳入候选:
                    # HA 2026 bleak_esphome v3(REMOTE_CACHING) 不解析描述符,
                    # 由 _try_subscribe 注入伪 CCCD 按句柄直写使能通知。
                    if has_notify and (has_ccc or uid.startswith(CHAR_UUID[:8])):
                        if uid.startswith(CHAR_UUID[:8]):
                            notify_candidates.append(ch)  # FFF1 最优先
                        elif "fff0" in svc_uid:
                            cand_fff0.append(ch)
                        else:
                            cand_other.append(ch)
                    if "read" in props:
                        if uid.startswith(CHAR_UUID[:8]):
                            read_char = ch  # 优先 FFF1
                        elif read_char is None:
                            read_char = ch
                    if "write" in props or "write-without-response" in props:
                        if uid.startswith(CHAR_UUID[:8]):
                            write_char = ch  # 优先 FFF1
                        elif write_char is None:
                            write_char = ch
            notify_candidates += cand_fff0 + cand_other
            self._write_char = write_char
            self._read_char = read_char
            _LOGGER.info(
                "SICN GATT 发现: write=%s read=%s 订阅候选=%s",
                write_char.uuid if write_char else None,
                read_char.uuid if read_char else None,
                [str(c.uuid)[:8] for c in notify_candidates],
            )

            # ---- 逐个尝试订阅 ----
            # 每个候选特征最多给 45s：内部会逐个句柄偏移试伪 CCCD，若代理
            # 全面失联则每次都是 GATT_TIMEOUT 级别的等待，不设总闸会卡几分钟。
            notify_char = None
            for cand in notify_candidates:
                try:
                    sub = await asyncio.wait_for(
                        self._try_subscribe(client, cand), timeout=45
                    )
                except asyncio.TimeoutError:
                    _LOGGER.info("SICN 订阅 %s 整体超时, 跳过", str(cand.uuid)[:8])
                    continue
                if sub is not None:
                    notify_char = sub
                    break
            self._notify_char = notify_char
            if notify_char is None:
                _LOGGER.info("SICN 无可用通知通道, 转读轮询+写响应模式")

            self.client = client
            self.connected = True
            self.data["connected"] = True
            self._notify()
            _LOGGER.info("SICN 已连接 %s", self.address)

            # ---- 连接后握手：用 response=True 并解析返回字节 ----
            try:
                await asyncio.sleep(1.0)
                resp = await self._raw_write(CMD_SYNC, capture=True)
                if resp:
                    _LOGGER.info("握手 01 03 响应: %s", bytes(resp).hex(" "))
                    self._parse(bytes(resp), "写响应")
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("握手失败: %s", err)

            # ---- 一次性暴力读取所有可读特征，定位真实数据存放处 ----
            try:
                for service in client.services:
                    for ch in service.characteristics:
                        if "read" in set(ch.properties):
                            try:
                                raw = await asyncio.wait_for(
                                    client.read_gatt_char(ch), timeout=5
                                )
                                if raw and bytes(raw) != b"\x00\x00\x00\x00":
                                    _LOGGER.info(
                                        "READ %s = %s",
                                        str(ch.uuid)[:8], bytes(raw).hex(" "),
                                    )
                                    self._parse(bytes(raw), "直接读")
                            except Exception:  # noqa: BLE001
                                pass
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("暴力读扫描异常: %s", err)

        # 阻塞直到断开, 然后退出由外层循环重连
        try:
            first_read = True
            while self.client and self.client.is_connected:
                # 未订阅到 FFF1 通知时: 轮询读取状态兜底
                sub_fff1 = (
                    self._notify_char is not None
                    and str(self._notify_char.uuid).lower().startswith(CHAR_UUID[:8])
                )
                # 没有 FFF1 通知通道时：周期性写同步命令 01 03，
                # 设备会把状态帧作为写响应回传（实测每帧都带 3001 状态），
                # 这比读 FFF1（只回显 01 03）有效得多。
                # 有 FFF1 通知通道时判定可以更宽松（设备在变化时才推，
                # 长时间没变化不等于断连）；无通道则靠重连抓快照，沿用 150s。
                stale_limit = 600 if sub_fff1 else 150
                if self._last_status and (time.monotonic() - self._last_status) > stale_limit:
                    _LOGGER.debug("SICN 状态超 %ds 未更新，断开重连以刷新", stale_limit)
                    break
                if sub_fff1:
                    # 通知通道已打通：设备自己每秒推 3001，无需再轮询写命令，
                    # 保持连接、定期确认还活着即可。
                    await asyncio.sleep(30.0)
                    continue
                if self._write_char is not None and not sub_fff1:
                    try:
                        async with self._write_lock:
                            # 加超时：代理偶发不返回写响应，卡住会拖死整个轮询
                            resp = await asyncio.wait_for(
                                self._raw_write(CMD_SYNC, capture=True), timeout=10
                            )
                        if resp:
                            if first_read:
                                _LOGGER.info(
                                    "SICN 轮询写首包: %s", bytes(resp).hex(" ")
                                )
                                first_read = False
                            self._parse(bytes(resp), "写响应")
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("SICN 轮询写失败: %s", err)
                elif self._read_char is not None:
                    try:
                        raw = await self.client.read_gatt_char(self._read_char)
                        if raw:
                            if first_read:
                                _LOGGER.info("READ(轮询首包) %s = %s",
                                             str(self._read_char.uuid)[:8],
                                             bytes(raw).hex(" "))
                                first_read = False
                            # 仅在非零时报“未知包”，避免电池式 00 噪声
                            if bytes(raw) != b"\x00\x00\x00\x00":
                                self._parse(bytes(raw), "直接读")
                    except Exception as err:  # noqa: BLE001
                        if first_read:
                            _LOGGER.info("READ 失败: %s", err)
                            first_read = False
                await asyncio.sleep(8.0)
        finally:
            await self._disconnect()

    # ---------------------------------------------------------------- 订阅辅助
    async def _try_subscribe(self, client, cand):
        """订阅一个特征, 返回成功订阅的特征对象或 None。

        若 GATT 表里没有 CCCD(HA 2026 bleak_esphome v3 代理连接不解析描述符),
        注入伪 CCCD 描述符(handle=特征句柄+1, 兜底+2), 后端会按句柄
        bluetooth_gatt_write_descriptor 直写 0100 使能通知。
        FFF1 订阅后写一次握手并等待通知到达, 验证句柄真的有效。
        """
        uid = str(cand.uuid).lower()
        is_fff1 = uid.startswith(CHAR_UUID[:8])
        try:
            from bleak.backends.descriptor import BleakGATTDescriptor
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("无法导入 BleakGATTDescriptor: %s", err)
            BleakGATTDescriptor = None

        descs = list(getattr(cand, "descriptors", []))
        if not any(str(d.uuid).lower() == CCCD_UUID for d in descs):
            # ---- 方案 A：BlueZ 的 AcquireNotify ----
            # 本机 hci0 直连时可用，走 GATT socket 收通知，**不需要 CCCD**。
            # 这正是本设备（FFF1 无描述符）唯一能收通知的方式。
            try:
                await asyncio.wait_for(
                    client.start_notify(
                        cand, self._on_notify, bluez={"use_start_notify": False}
                    ),
                    timeout=GATT_TIMEOUT,
                )
            except TypeError:
                pass  # 非 BlueZ 后端不认识这个参数
            except asyncio.TimeoutError:
                _LOGGER.debug("SICN AcquireNotify %s 超时", uid[:8])
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("SICN AcquireNotify %s 失败: %s", uid[:8], err)
            else:
                if not is_fff1:
                    _LOGGER.info("SICN 订阅成功(AcquireNotify): %s", uid[:8])
                    return cand
                self._notify_event.clear()
                try:
                    await self._raw_write(CMD_SYNC, capture=True)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await asyncio.wait_for(self._notify_event.wait(), timeout=6)
                    _LOGGER.info("SICN 订阅成功(AcquireNotify), 通知已到达: %s", uid[:8])
                    return cand
                except asyncio.TimeoutError:
                    _LOGGER.info("SICN AcquireNotify 无通知，改试伪 CCCD")
                    try:
                        await asyncio.wait_for(
                            client.stop_notify(cand), timeout=5
                        )
                    except Exception:  # noqa: BLE001
                        pass

            if BleakGATTDescriptor is None:
                _LOGGER.info("SICN %s 无 CCCD 且无法注入伪描述符", uid[:8])
                return None
            injected = None
            # ESP32 代理的 GATT 缓存可能不全，CCCD 句柄不一定紧邻特征值，
            # 所以在特征句柄附近逐个试写 0100，任一成功即可使能通知。
            # 注意必须包含 offset=0：实测 cand.handle=9，FFF1 值句柄是 8，
            # CCCD 正好落在 9——旧列表 (-1,1,2,3,4,5) 命中的是 8 和 10~14，
            # 偏偏跳过唯一正确的 9，导致一直「CCCD 全部失败」。
            for off in (0, 1, -1, 2, 3, 4, 5, 6):
                fake = BleakGATTDescriptor(None, cand.handle + off, CCCD_UUID, cand)
                try:
                    cand._descriptors[fake.handle] = fake
                except Exception as err:  # noqa: BLE001
                    _LOGGER.info("SICN 注入伪 CCCD 失败: %s", err)
                    return None
                try:
                    await asyncio.wait_for(
                        client.start_notify(cand, self._on_notify),
                        timeout=GATT_TIMEOUT,
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.info("SICN 伪CCCD(h+%d)订阅 %s 失败: %s",
                                 off, uid[:8], err)
                    cand._descriptors.pop(fake.handle, None)
                    try:
                        await asyncio.wait_for(
                            client.stop_notify(cand), timeout=5
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                if not is_fff1:
                    _LOGGER.info("SICN 订阅成功(伪CCCD h+%d): %s", off, uid[:8])
                    return cand
                # FFF1: 写握手并等通知到达, 验证句柄有效
                self._notify_event.clear()
                try:
                    await self._raw_write(CMD_SYNC, capture=True)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("验证握手写失败: %s", err)
                try:
                    await asyncio.wait_for(self._notify_event.wait(), timeout=6)
                    _LOGGER.info("SICN 订阅成功(伪CCCD h+%d), 通知已到达: %s",
                                 off, uid[:8])
                    return cand
                except asyncio.TimeoutError:
                    _LOGGER.info("SICN 伪CCCD(h+%d)无通知, 换句柄重试", off)
                    cand._descriptors.pop(fake.handle, None)
                    try:
                        await asyncio.wait_for(
                            client.stop_notify(cand), timeout=5
                        )
                    except Exception:  # noqa: BLE001
                        pass

            # ---- 最终验证：CCCD 全写失败，但通知回调可能已经挂上了 ----
            # 实测（2026-11-01）：bleak_esphome 下 start_notify 因无 CCCD 报
            # "Write not permitted"/"Invalid handle"，可 BlueZ 的 Notify 信号
            # 照样送达回调——设备状态帧确实从 _on_notify 进来了。
            # 之前直接 return None 导致误判「无通知通道」，退化成等写响应，
            # 而设备写响应恒为空，数据就此僵死。这里以「写一次握手能否收到
            # 通知」作为真正的判定依据。
            if is_fff1:
                self._notify_event.clear()
                try:
                    await self._raw_write(CMD_SYNC, capture=True)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("SICN 通知验证握手失败: %s", err)
                try:
                    await asyncio.wait_for(self._notify_event.wait(), timeout=8)
                    _LOGGER.info(
                        "SICN 订阅成功(CCCD 写失败但通知在流): %s", uid[:8]
                    )
                    return cand
                except asyncio.TimeoutError:
                    _LOGGER.info("SICN 确认无通知通道(8s 无帧), 转写响应/读轮询")
            return None
        # 真有 CCCD: 正常订阅
        try:
            await asyncio.wait_for(
                client.start_notify(cand, self._on_notify), timeout=GATT_TIMEOUT
            )
            _LOGGER.info("SICN 订阅成功: %s", uid[:8])
            return cand
        except Exception as err:  # noqa: BLE001
            _LOGGER.info("SICN 订阅 %s 失败: %s", uid[:8], err)
            return None

    async def _disconnect(self):
        async with self._conn_lock:
            if self.client:
                try:
                    if self.client.is_connected:
                        await self.client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    if self._notify_char is not None:
                        await asyncio.wait_for(
                            self.client.stop_notify(self._notify_char), timeout=5
                        )
                except Exception:  # noqa: BLE001
                    pass
                self.client = None
            self._notify_char = None
            self._write_char = None
            self._read_char = None
            self.connected = False
            self.data["connected"] = False
            self._notify()

    # ---------------------------------------------------------------- 通知解析
    def _on_notify(self, _handle, data):
        self._notify_event.set()
        try:
            self._parse(bytes(data), "通知")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("解析通知失败: %s data=%s", err, data.hex() if data else "")

    def _parse(self, pkt: bytes, src: str = "?"):
        if len(pkt) >= 4:
            self._last_status = time.monotonic()
        # 通知通道打通后设备每秒推一帧，原始字节降到 debug 免得刷屏
        _LOGGER.debug("SICN 原始帧[%s](%d字节): %s", src, len(pkt), pkt.hex(" "))
        if len(pkt) < 2:
            return
        # 3001 当前温度: 3001 MODE UNIT T_LO T_HI [...]
        if pkt[0] == 0x30 and pkt[1] == 0x01 and len(pkt) >= 6:
            mode = pkt[2]
            unit = "F" if pkt[3] == 1 else "C"
            raw = (pkt[5] << 8) | pkt[4]
            temp = raw / 10.0
            cur_c = round((temp - 32.0) / 1.8 if unit == "F" else temp, 1)
            # 只在真有变化时通知实体刷新（设备每秒都在推同样内容）
            if (
                self.data.get("current") != cur_c
                or self.data.get("mode") != mode
                or self.data.get("unit") != unit
            ):
                self.data["current"] = cur_c
                self.data["mode"] = mode
                self.data["unit"] = unit
                _LOGGER.info(
                    "SICN 状态: 当前=%.1f℃ 模式=%s 单位=%s",
                    cur_c, MODE_TEXT.get(mode, str(mode)), unit,
                )
                self._notify()
        # 3101 目标温度回显: 3101 LO HI (无偏移, 当前单位下的温度)
        elif pkt[0] == 0x31 and pkt[1] == 0x01 and len(pkt) >= 4:
            raw = (pkt[3] << 8) | pkt[2]
            temp = raw / 10.0
            unit = self.data.get("unit", "C")
            tgt_c = round((temp - 32.0) / 1.8 if unit == "F" else temp, 1)
            if self.data.get("target") != tgt_c:
                self.data["target"] = tgt_c
                _LOGGER.info("SICN 目标温度: %.1f℃", tgt_c)
                self._notify()
        # 3003/3002 历史: 目前不展示, 仅记录日志便于调试
        elif pkt[0] == 0x30 and (pkt[1] in (0x02, 0x03)):
            _LOGGER.debug("SICN 历史包: %s", pkt.hex(" "))
        else:
            _LOGGER.debug("SICN 未知包: %s", pkt.hex(" "))

    # ---------------------------------------------------------------- 写指令
    async def _raw_write(self, data: bytes, capture: bool = False):
        """写指令。capture=True 时用 response=True 并返回设备响应字节。"""
        if not self.client or not self.client.is_connected:
            raise RuntimeError("未连接")
        target = self._write_char or CHAR_UUID
        if capture:
            try:
                resp = await asyncio.wait_for(
                    self.client.write_gatt_char(target, data, response=True),
                    timeout=10,
                )
                return resp
            except asyncio.TimeoutError:
                _LOGGER.debug("response=True 写超时, 转无响应写")
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("response=True 写失败, 转无响应写: %s", err)
        await asyncio.wait_for(
            self.client.write_gatt_char(target, data, response=False), timeout=10
        )
        return None

    async def _raw_write_once(self, data: bytes, response: bool = True):
        """只写一次，**绝不做「带响应失败→改无响应重发」的兜底**。

        兜底逻辑对幂等命令（31 01 设温、01 03 同步）无害，但对
        `31 02 切换单位` 是致命的：切过去再切一次就切回来了，
        表现就是「按了按钮没反应」。
        """
        if not self.client or not self.client.is_connected:
            raise RuntimeError("未连接")
        target = self._write_char or CHAR_UUID
        return await asyncio.wait_for(
            self.client.write_gatt_char(target, data, response=response), timeout=10
        )

    async def async_set_target(self, temp_c: float):
        """设目标温度 (temp_c 为摄氏度)。

        步进按设备当前档位落实：
          - ℃ 档：0.5℃ 步进
          - ℉ 档：1℉ 步进（设备在华氏档按整数华氏显示）
        """
        unit = self.data.get("unit", "C")
        if unit == "F":
            dev_f = float(round(temp_c * 1.8 + 32.0))   # 1℉ 步进
            temp_c = (dev_f - 32.0) / 1.8
            value = int(round(dev_f * 10))
        else:
            temp_c = round(temp_c * 2) / 2.0            # 0.5℃ 步进
            value = int(round(temp_c * 10))
        cmd = bytes([0x31, 0x01, value & 0xFF, (value >> 8) & 0xFF])
        # 乐观更新：设备不一定回 3101，先让 UI 立刻反映用户设定
        self.data["target"] = round(float(temp_c), 1)
        self._notify()
        _LOGGER.info("SICN 设温下发: %.1f℃ (下发值 %d, 帧 %s)", temp_c, value, cmd.hex(" "))
        async with self._write_lock:
            resp = await self._raw_write(cmd, capture=True)
        if resp:
            _LOGGER.info("设温 31 01 响应: %s", bytes(resp).hex(" "))
            self._parse(bytes(resp), "写响应")

    async def async_toggle_unit(self):
        """切换设备显示单位 (31 02)。toggle 命令，必须只发一次。

        设备切换单位时会把目标温度弄乱（实测 28.0℃ 切到 ℉ 后变成 28.3℃，
        重启后再切又出现 26.0℃），所以切换后要主动按新单位把原目标温度
        换算写回：
          - ℉ 档目标温度按 1℉ 步进取整
          - ℃ 档目标温度按 0.5℃ 步进取整
        这样切单位只是换个刻度看，设定值本身不被重置。
        """
        cur_c = self.data.get("target")
        cur = self.data.get("unit", "C")
        expect = "F" if cur == "C" else "C"

        async with self._write_lock:
            await asyncio.wait_for(
                self._raw_write_once(CMD_UNIT, response=True), timeout=10
            )
        _LOGGER.info("已发送切换单位命令 31 02 (原单位 %s → %s)", cur, expect)

        # 等设备推来新单位的 3001 帧，最多 3s（13:xx 实测 40ms 就到）
        for _ in range(30):
            if self.data.get("unit") == expect:
                break
            await asyncio.sleep(0.1)
        else:
            _LOGGER.info("切换单位后 3s 未见新单位帧，仍按 %s 换算写回", expect)

        if cur_c is None:
            _LOGGER.info("目标温度未知，跳过写回")
            return

        # 按目标单位算显示值（并落实步进），再换算回下发用的值
        if expect == "F":
            disp = float(round(cur_c * 1.8 + 32.0))   # 1℉ 步进
            new_c = (disp - 32.0) / 1.8
        else:
            disp = round(cur_c * 2) / 2.0             # 0.5℃ 步进
            new_c = disp

        value = int(round(disp * 10))
        cmd = bytes([0x31, 0x01, value & 0xFF, (value >> 8) & 0xFF])
        async with self._write_lock:
            await asyncio.wait_for(
                self._raw_write_once(cmd, response=True), timeout=10
            )
        self.data["target"] = round(new_c, 1)
        self._notify()
        _LOGGER.info(
            "切换单位后写回目标温度: %s°%s (≈%.1f℃, 下发值 %d)",
            disp, expect, new_c, value,
        )

    async def async_refresh(self):
        async with self._write_lock:
            await self._raw_write(CMD_SYNC)
