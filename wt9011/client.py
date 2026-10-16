"""WT9011DCL-BT50 的 Linux BLE 客户端（bleak / BlueZ 实现）。

对应 Windows 上位机里的 WinBlue 后端（Windows.Devices.Bluetooth），
这里换成跨平台的 BlueZ + bleak。
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from . import protocol as P


class WitError(RuntimeError):
    pass


def _bluetooth_connected_state(address: str) -> Optional[bool]:
    """借助 bluetoothctl 判断该地址当前是否已被系统/其它程序连上（查不到返回 None）。"""
    import subprocess

    try:
        out = subprocess.run(["bluetoothctl", "info", address],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line.strip().startswith("Connected:"):
            return line.split(":", 1)[1].strip().lower() == "yes"
    return None


def trust_device(address: str) -> bool:
    """把设备标记为 trusted —— BlueZ 会把 trusted 设备永久保留在缓存里，
    这样以后可以脱离扫描直接 ``BleakClient(address)`` 连接（模块不广播时也能连）。"""
    import subprocess

    try:
        subprocess.run(["bluetoothctl", "trust", address],
                       capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def is_trusted(address: str) -> Optional[bool]:
    import subprocess

    try:
        out = subprocess.run(["bluetoothctl", "info", address],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line.strip().startswith("Trusted:"):
            return line.split(":", 1)[1].strip().lower() == "yes"
    return None
def clear_pending_connection(address: str, wait: float = 2.0) -> bool:
    """清理 BlueZ 里卡住的连接（GNOME 蓝牙面板点过 / 上次异常退出留下的半连接）。

    在 GNOME 蓝牙设置里点“连接”会占用设备，之后第三方程序连接就会一直报
    ``org.bluez.Error.InProgress``；先 disconnect 掉再连就正常了。
    """
    import subprocess
    import time

    try:
        subprocess.run(["bluetoothctl", "disconnect", address],
                       capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    time.sleep(wait)
    return _bluetooth_connected_state(address) is not True


class WitClient:
    """维特智能 BLE 5.0 传感器客户端（WT9011DCL-BT50 / BWT901BLE5.0 等）。"""

    def __init__(
        self,
        address: Optional[str] = None,
        mode: int = 0,
        on_sample: Optional[Callable[[P.Sample], None]] = None,
        on_register: Optional[Callable[[P.RegisterFrame], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_disconnect: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.address = address
        self.mode = mode  # 0=加速度/角速度/角度，1=位移/位移速度/角度
        self.on_sample = on_sample
        self.on_register = on_register
        self.on_status = on_status
        self.on_disconnect = on_disconnect
        self._manual_disconnect = False

        self.client: Optional[BleakClient] = None
        self.device: Optional[BLEDevice] = None
        self.parser = P.PacketParser()
        self.samples = 0
        self.corrupt_frames = 0
        self.last_sample: Optional[P.Sample] = None
        self.started_at = 0.0

        self._queue: "asyncio.Queue[P.Sample]" = asyncio.Queue(maxsize=2048)
        self._reg_futures: Dict[int, asyncio.Future] = {}
        self._notify_char = None
        self._write_char = None

    # ------------------------------------------------------------ 状态/日志
    def _log(self, msg: str) -> None:
        if self.on_status:
            self.on_status(msg)

    @property
    def is_connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    @property
    def sample_rate(self) -> float:
        if not self.started_at:
            return 0.0
        dt = time.time() - self.started_at
        return self.samples / dt if dt > 0 else 0.0

    # ------------------------------------------------------------ 扫描/连接
    @staticmethod
    async def scan(timeout: float = 8.0, name_prefixes: Sequence[str] = P.NAME_PREFIXES,
                   match_service: bool = True) -> List[Tuple[BLEDevice, AdvertisementData, int]]:
        """扫描附近的维特设备，返回 [(BLEDevice, AdvertisementData, rssi)]，RSSI 高的在前。

        设备名以 WT/BWT 开头即认为是维特设备（上位机用同样的规则过滤）；
        打开 match_service 时也接受广播里带 FFE5 服务的设备。
        """
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        result: List[Tuple[BLEDevice, AdvertisementData, int]] = []
        for device, adv in found.values():
            name = device.name or adv.local_name or ""
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            if name.upper().startswith(tuple(p.upper() for p in name_prefixes)) or (
                match_service and P.SERVICE_UUID.lower() in uuids
            ):
                result.append((device, adv, adv.rssi))
        result.sort(key=lambda item: item[2], reverse=True)
        return result

    async def connect(self, address: Optional[str] = None, scan_timeout: float = 10.0,
                      connect_timeout: float = 20.0, force_scan: bool = False,
                      attempts: int = 3) -> str:
        """连接设备。

        默认策略（实测维特 BLE 模块连过一次后往往不再广播，直接扫描会"找不到"）：
          1. 有地址 → 先不扫描，直接用地址连（BlueZ 缓存里已有该设备就能连上）；
          2. 直连失败 → 按地址扫描，再退化为按名字前缀扫描（WT/BWT）；
          3. 没有地址 → 扫描并连接信号最强的维特设备。
        """
        target = address or self.address
        if target:
            self.address = target
            await self._ensure_trusted(target)
            # 设备若显示"已连接"而我们手里没有连接（上次进程被 kill / 别的程序占用），
            # 先释放掉这条残留链路，否则新连接会一直失败或扫不到设备。
            loop = asyncio.get_running_loop()
            if await loop.run_in_executor(None, _bluetooth_connected_state, target) is True:
                self._log("设备当前状态为“已连接”（可能是上次未正常断开的残留），先释放…")
                await loop.run_in_executor(None, clear_pending_connection, target)
            last_error: Optional[Exception] = None
            for round_no in range(1, max(1, attempts) + 1):
                if not force_scan:
                    try:
                        return await self._connect_by_address(target, connect_timeout)
                    except Exception as exc:  # noqa: BLE001
                        last_error = exc
                        self._log(f"直接按地址连接失败（{type(exc).__name__}: {exc}），改为扫描查找…")
                device = await self._find_by_scan(target, scan_timeout)
                if device is not None:
                    return await self.connect_device(device, connect_timeout)
                if round_no < attempts:
                    self._log(f"第 {round_no}/{attempts} 轮没找到设备，3 秒后重试"
                              f"（确认模块已开机、指示灯在闪）…")
                    await asyncio.sleep(3)
            state = _bluetooth_connected_state(target)
            extra = ""
            if state is True:
                extra = ("\n  4) 该设备当前已被系统/其它程序占用（BlueZ 显示已连接）："
                         "请在 GNOME 蓝牙设置或 bluetoothctl 里先断开它。")
            raise WitError(
                f"连接 {target} 失败（{last_error}）。请确认：\n"
                f"  1) 传感器已开机（指示灯闪）；\n"
                f"  2) 没有被手机 App、Windows 上位机或 GNOME 蓝牙设置占用；\n"
                f"  3) 模块离电脑近一些（RSSI 太低会连不上）；\n"
                f"  4) 必要时给模块断电重启一次再试。{extra}"
            )
            return await self.connect_device(device, connect_timeout)

        self._log("扫描维特设备（WT/BWT 开头）…")
        candidates = await self.scan(timeout=scan_timeout)
        if not candidates:
            raise WitError("没有扫描到维特设备，请确认传感器已开机且未被其它主机占用")
        device, adv, rssi = candidates[0]
        self._log(f"发现 {device.name or device.address} (RSSI {rssi})")
        return await self.connect_device(device, connect_timeout)

    async def _ensure_trusted(self, address: str) -> None:
        """确保设备被 BlueZ 记住（trusted 不会被清除），失败也不影响连接。"""
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(None, is_trusted, address)
        if state is False:
            if await loop.run_in_executor(None, trust_device, address):
                self._log("已把设备标记为 trusted（以后可以免扫描直连）")

    async def _connect_by_address(self, address: str, connect_timeout: float = 20.0) -> str:
        """跳过扫描，直接用地址连接（BlueZ 已知设备）。

        遇到 ``InProgress``（BlueZ 里还有半连接/别的程序正在连）时，
        自动先 disconnect 清掉再重试，最多 3 次。
        """
        self._log(f"连接 {address} …")
        last_exc: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                client = BleakClient(address, timeout=connect_timeout,
                                     disconnected_callback=self._on_disconnected)
                await client.connect()
                return await self._after_connect(client, address)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                text = f"{type(exc).__name__}: {exc}"
                if "InProgress" in text or "in progress" in text.lower():
                    self._log("BlueZ 里还有一个占用的连接，正在清理后重试…")
                    await asyncio.get_running_loop().run_in_executor(
                        None, clear_pending_connection, address)
                    continue
                if attempt < 3 and ("Not connected" in text or "Failed to connect" in text
                                    or "le-connection-abort" in text):
                    self._log(f"连接未成功，2 秒后重试（{attempt}/3）…")
                    await asyncio.sleep(2)
                    continue
                raise
        raise WitError(f"连接 {address} 失败：{last_exc}")

    async def _find_by_scan(self, address: str, scan_timeout: float, attempts: int = 2):
        """先按地址精确扫描，再退化为按名字前缀扫描。"""
        for i in range(attempts):
            self._log(f"扫描查找 {address} …（第 {i + 1}/{attempts} 次）")
            device = await BleakScanner.find_device_by_address(address, timeout=scan_timeout)
            if device is not None:
                return device
        self._log("按地址没扫到，尝试按名字前缀（WT/BWT）扫描…")
        candidates = await self.scan(timeout=scan_timeout)
        return candidates[0][0] if candidates else None

    async def connect_device(self, device: BLEDevice, connect_timeout: float = 20.0) -> str:
        client = BleakClient(device, timeout=connect_timeout,
                             disconnected_callback=self._on_disconnected)
        await client.connect()
        return await self._after_connect(client, device.name or device.address, device)

    async def _after_connect(self, client: BleakClient, label: str,
                             device: Optional[BLEDevice] = None) -> str:
        """连接成功后的公共收尾：解析特征值 + 订阅通知。"""
        self.client = client
        if device is not None:
            self.device = device
        self.address = client.address
        self.parser.reset()
        self._notify_char, self._write_char = self._resolve_characteristics()
        await self.client.start_notify(self._notify_char, self._on_notify)
        self.started_at = time.time()
        self.samples = 0
        self._log(f"已连接 {label}")
        return self.address

    def _resolve_characteristics(self):
        """按 FFE5 服务找 FFE4(notify) / FFE9(write)；找不到就退化为按属性匹配。"""
        svc = self.client.services.get_service(P.SERVICE_UUID)
        notify = write = None
        if svc:
            for char in svc.characteristics:
                if char.uuid.lower() == P.NOTIFY_UUID.lower():
                    notify = char
                elif char.uuid.lower() == P.WRITE_UUID.lower():
                    write = char
        if notify is None or write is None:
            for s in self.client.services:
                for char in s.characteristics:
                    props = [p.lower() for p in char.properties]
                    if notify is None and ("notify" in props or "indicate" in props):
                        notify = char
                    if write is None and ("write" in props or "write-without-response" in props):
                        write = char
        if notify is None or write is None:
            raise WitError("设备上没有找到可用的 notify/write 特征值，可能不是维特 BLE 5.0 模块")
        return notify, write

    async def disconnect(self) -> None:
        self._manual_disconnect = True
        if self.client and self.client.is_connected:
            try:
                if self._notify_char is not None:
                    await self.client.stop_notify(self._notify_char)
            except Exception:
                pass
            await self.client.disconnect()
        self._log("已断开")
        self._fire_disconnect(True)

    def _fire_disconnect(self, manual: bool) -> None:
        if self.on_disconnect:
            self.on_disconnect(manual)

    def _on_disconnected(self, _client: BleakClient) -> None:
        manual = self._manual_disconnect
        self._manual_disconnect = False
        if not manual:
            self._log("连接意外断开（对端掉线或超出范围）")
        self._fire_disconnect(manual)

    # ------------------------------------------------------------ 通知回调
    def _on_notify(self, _char, data: bytearray) -> None:
        for frame in self.parser.feed(bytes(data)):
            if frame[1] == P.FLAG_SENSOR:
                sample = P.parse_sensor_frame(frame, mode=self.mode, t=time.time())
                if not P.is_plausible(sample):
                    self.corrupt_frames += 1
                    if self.corrupt_frames <= 5:
                        self._log(f"丢弃 1 帧异常数据（错帧累计 {self.corrupt_frames}，多是 BLE 丢字节导致）")
                    continue
                self.samples += 1
                self.last_sample = sample
                if self.on_sample:
                    self.on_sample(sample)
                try:
                    self._queue.put_nowait(sample)
                except asyncio.QueueFull:
                    _ = self._queue.get_nowait()
                    self._queue.put_nowait(sample)
            else:
                rf = P.parse_register_frame(frame)
                fut = self._reg_futures.get(rf.start_reg)
                if fut is not None and not fut.done():
                    fut.set_result(rf)
                if self.on_register:
                    self.on_register(rf)

    async def stream(self):
        """异步迭代传感器数据帧。"""
        while True:
            yield await self._queue.get()

    # ------------------------------------------------------------ 收发
    async def send(self, payload: bytes) -> None:
        if not self.is_connected:
            raise WitError("设备未连接")
        await self.client.write_gatt_char(self._write_char, payload)

    async def read_registers(self, reg, timeout: float = 2.0, retries: int = 3) -> P.RegisterFrame:
        """读取寄存器：返回从该地址开始的 8 个寄存器值（0x55 0x71 回传）。"""
        addr = P.resolve_register(reg).addr
        last_err: Optional[Exception] = None
        for _ in range(max(1, retries)):
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._reg_futures[addr] = fut
            try:
                await self.send(P.encode_read(addr))
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError as exc:
                last_err = exc
            finally:
                self._reg_futures.pop(addr, None)
        raise WitError(f"读取寄存器 {reg} 超时") from last_err

    async def read_value(self, reg, timeout: float = 2.0) -> int:
        """读取单个寄存器原始值。"""
        info = P.resolve_register(reg)
        rf = await self.read_registers(info.addr, timeout=timeout)
        value = rf.get(info.addr)
        if value is None:
            raise WitError(f"回传数据里没有寄存器 0x{info.addr:02X}")
        return value

    async def unlock(self) -> None:
        await self.send(P.CMD_UNLOCK)

    async def write_register(self, reg, value: int, save: bool = True, delay: float = 0.1) -> None:
        """写寄存器：先解锁 → 写值 → （默认）保存。"""
        info = P.resolve_register(reg)
        await self.unlock()
        await asyncio.sleep(delay)
        await self.send(P.encode_write(info.addr, value))
        await asyncio.sleep(delay)
        if save:
            await self.send(P.CMD_SAVE)
            await asyncio.sleep(delay)

    async def save(self) -> None:
        await self.send(P.CMD_SAVE)

    async def restore_default(self) -> None:
        await self.send(P.CMD_RESTORE_DEFAULT)

    async def reboot(self) -> None:
        await self.send(P.CMD_REBOOT)

    # ------------------------------------------------------------ 校准/设置
    async def acc_calibration(self) -> None:
        """"加速度校准：模块需水平静止放置。"""
        await self.unlock()
        await asyncio.sleep(0.1)
        await self.send(P.CMD_ACC_CALIB)
        await asyncio.sleep(0.1)
        await self.send(P.CMD_SAVE)

    async def mag_calibration_start(self) -> None:
        """进入磁场校准（随后需绕三轴各转三圈）。"""
        await self.unlock()
        await asyncio.sleep(0.2)
        await self.send(P.CMD_MAG_CALIB_START)

    async def mag_calibration_finish(self) -> None:
        """退出磁场校准并保存。"""
        await self.unlock()
        await asyncio.sleep(0.2)
        await self.send(P.CMD_CALIB_EXIT)
        await asyncio.sleep(0.1)
        await self.send(P.CMD_SAVE)

    async def yaw_zero(self) -> None:
        """Z 轴角度归零（协议要求先切到六轴算法）。"""
        await self.unlock()
        await asyncio.sleep(0.1)
        await self.send(P.CMD_YAW_ZERO)
        await asyncio.sleep(0.1)
        await self.send(P.CMD_SAVE)

    async def angle_reference(self) -> None:
        """把当前姿态设为角度参考。"""
        await self.unlock()
        await asyncio.sleep(0.1)
        await self.send(P.CMD_ANGLE_REF)
        await asyncio.sleep(0.1)
        await self.send(P.CMD_SAVE)

    async def set_rate(self, rate: int) -> None:
        """设置回传速率，rate 取 0x01..0x0B（见 REGISTERS 中 RATE 选项）。"""
        await self.write_register(0x03, rate)

    async def set_bandwidth(self, level: int) -> None:
        await self.write_register(0x1F, level)

    async def set_orientation(self, horizontal: int) -> None:
        await self.write_register(0x23, horizontal)

    async def set_algorithm(self, axis6: int) -> None:
        await self.write_register(0x24, axis6)

    async def set_output_mode(self, mode: int) -> None:
        """0=加速度+角速度+角度，1=位移+位移速度+角度。"""
        await self.write_register(0x96, mode)
        self.mode = mode

    async def set_device_name(self, name: str) -> None:
        """蓝牙名称：指令形如 ``WTWT12345678\\r\\n``（第一个 WT 固定，第二个 WT 是名字前缀）。"""
        payload = ("WT" + name).encode("ascii") + b"\r\n"
        await self.send(payload)

    async def set_time(self, when: Optional[time.struct_time] = None) -> None:
        import datetime

        now = datetime.datetime.now() if when is None else datetime.datetime(*when[:6])
        yymm = ((now.year % 100) // 10 << 4 | (now.year % 10)) << 8 | (now.month // 10 << 4 | now.month % 10)
        ddh = (now.day // 10 << 4 | now.day % 10) << 8 | (now.hour // 10 << 4 | now.hour % 10)
        mms = (now.minute // 10 << 4 | now.minute % 10) << 8 | (now.second // 10 << 4 | now.second % 10)
        ms = int(now.microsecond / 1000)
        await self.write_register(0x30, yymm, save=False)
        await self.write_register(0x31, ddh, save=False)
        await self.write_register(0x32, mms, save=False)
        await self.write_register(0x33, ms)

    # ------------------------------------------------------------ 便捷读取
    async def read_battery(self) -> dict:
        """电量：刚连上时模块偶尔会回 0，这里自动重读一次。"""
        raw = await self.read_value(0x64)
        if raw <= 0:
            await asyncio.sleep(0.2)
            raw = await self.read_value(0x64)
        return {"raw": raw, "voltage": raw / 100.0, "percent": P.battery_percent(raw)}

    async def read_version(self) -> str:
        """版本号：0x2E = (Version1L, Version1H)，0x2F = (Version2L, Version2H)。"""
        rf = await self.read_registers(0x2E)
        return P.version_string(rf.values[0], rf.values[1])

    async def read_temperature(self) -> float:
        return await self.read_value(0x40) * P.TEMP_SCALE

    async def read_magnetometer(self, ui_unit: bool = False) -> Tuple[float, float, float]:
        rf = await self.read_registers(0x3A)
        hx, hy, hz = rf.values[0], rf.values[1], rf.values[2]
        div = P.MAG_UI_DIVISOR if ui_unit else 1.0
        return hx / div, hy / div, hz / div

    async def read_quaternion(self) -> Tuple[float, float, float, float]:
        rf = await self.read_registers(0x51)
        return tuple(v * P.QUAT_SCALE for v in rf.values[:4])  # type: ignore[return-value]

    async def read_device_info(self) -> dict:
        """一次性读取电量 / 版本 / 温度 / 磁场，用于界面初始化。"""
        info: dict = {}
        try:
            info["battery"] = await self.read_battery()
        except Exception as exc:
            info["battery_error"] = str(exc)
        try:
            info["version"] = await self.read_version()
        except Exception as exc:
            info["version_error"] = str(exc)
        try:
            info["temperature"] = await self.read_temperature()
        except Exception as exc:
            info["temperature_error"] = str(exc)
        try:
            info["magnetometer"] = await self.read_magnetometer()
        except Exception as exc:
            info["magnetometer_error"] = str(exc)
        return info


async def _demo() -> None:
    """python -m wt9011.client —— 扫描 → 连接 → 打印 20 帧数据。"""
    client = WitClient(on_status=print)
    print("附近设备：")
    for dev, adv, rssi in await WitClient.scan(timeout=8):
        print(f"  {dev.address}  {dev.name!r}  RSSI={rssi}")
    await client.connect()
    try:
        n = 0
        async for s in client.stream():
            print(f"{s.t:.3f}  acc=({s.ax:6.2f},{s.ay:6.2f},{s.az:6.2f})g  "
                  f"gyro=({s.gx:8.2f},{s.gy:8.2f},{s.gz:8.2f})°/s  "
                  f"angle=({s.roll:8.2f},{s.pitch:8.2f},{s.yaw:8.2f})°")
            n += 1
            if n >= 20:
                break
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(_demo())
