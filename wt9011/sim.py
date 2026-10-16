"""模拟设备：没有硬件时用来开发/演示界面（--mock）。

对外接口与 WitClient 保持一致（子集），产生的数据采用同一套换算系数，
方便在没有传感器时验证解析、绘图、记录链路。
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Callable, Optional, Tuple

from . import protocol as P


class SimulatedWitClient:
    def __init__(
        self,
        on_sample: Optional[Callable[[P.Sample], None]] = None,
        on_register: Optional[Callable[[P.RegisterFrame], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_disconnect: Optional[Callable[[bool], None]] = None,
        rate_hz: float = 10.0,
        mode: int = 0,
    ) -> None:
        self.on_sample = on_sample
        self.on_register = on_register
        self.on_status = on_status
        self.on_disconnect = on_disconnect
        self.mode = mode
        self.rate_hz = rate_hz
        self.address = "MOCK:00:11:22:33:44:55"
        self._task: Optional[asyncio.Task] = None
        self._connected = False
        self._t0 = 0.0
        self._regs = {0x03: 0x06, 0x1F: 0x0004, 0x23: 0, 0x24: 0, 0x96: mode,
                      0x2E: 0x0103, 0x2F: 0x89D8, 0x40: 2650, 0x64: 0x0194}
        self.samples = 0

    # ---------------- 生命周期
    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def sample_rate(self) -> float:
        return self.rate_hz

    def _log(self, msg: str) -> None:
        if self.on_status:
            self.on_status(f"[模拟] {msg}")

    async def connect(self, *_args, **_kwargs) -> str:
        self._connected = True
        self._t0 = time.time()
        self.samples = 0
        self._log("已连接模拟设备")
        self._task = asyncio.create_task(self._run())
        return self.address

    async def connect_device(self, *_args, **_kwargs) -> str:  # 兼容真实客户端接口
        return await self.connect()

    async def disconnect(self) -> None:
        self._connected = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._log("已断开")
        if self.on_disconnect:
            self.on_disconnect(True)

    async def stream(self):
        queue: "asyncio.Queue[P.Sample]" = asyncio.Queue(maxsize=256)
        self.on_sample = queue.put_nowait
        try:
            while True:
                yield await queue.get()
        finally:
            self.on_sample = None

    # ---------------- 数据产生
    async def _run(self) -> None:
        period = 1.0 / max(self.rate_hz, 0.1)
        while self._connected:
            t = time.time() - self._t0
            s = P.Sample(t=time.time(), mode=self.mode)
            if self.mode == 1:
                s.pos_e = 120 * math.sin(t * 1.5)
                s.pos_n = 80 * math.cos(t * 1.5)
                s.pos_u = 10 * math.sin(t * 0.7)
                s.vel_e = 180 * math.cos(t * 1.5)
                s.vel_n = -120 * math.sin(t * 1.5)
                s.vel_u = 7 * math.cos(t * 0.7)
            else:
                s.ax = 0.35 * math.sin(t * 2.1)
                s.ay = 0.35 * math.cos(t * 1.7)
                s.az = 1.0 + 0.08 * math.sin(t * 3.3)
                s.gx = 42 * math.sin(t * 1.3)
                s.gy = 30 * math.cos(t * 0.9)
                s.gz = 12 * math.sin(t * 0.5)
            s.roll = 25 * math.sin(t * 0.9)
            s.pitch = 15 * math.cos(t * 0.7)
            s.yaw = (t * 30) % 360 - 180
            self.samples += 1
            if self.on_sample:
                self.on_sample(s)
            await asyncio.sleep(period)

    # ---------------- 指令（只记录，不真正下发）
    async def send(self, payload: bytes) -> None:
        self._log(f"发送指令 {payload.hex(' ').upper()}")

    async def write_register(self, reg, value: int, save: bool = True, delay: float = 0.1) -> None:
        info = P.resolve_register(reg)
        self._regs[info.addr] = value & 0xFFFF
        self._log(f"写寄存器 {info.name} = {value}（保存={save}）")
        if info.addr == 0x03:
            self.rate_hz = {1: 0.1, 2: 0.5, 3: 1, 4: 2, 5: 5, 6: 10, 7: 20, 8: 50, 9: 100, 0x0B: 200}.get(value, self.rate_hz)
        if info.addr == 0x96:
            self.mode = value

    async def read_registers(self, reg, timeout: float = 2.0, retries: int = 1) -> P.RegisterFrame:
        addr = P.resolve_register(reg).addr
        values = [self._regs.get(addr + i, 0) for i in range(8)]
        frame = P.RegisterFrame(start_reg=addr, values=values)
        if self.on_register:
            self.on_register(frame)
        await asyncio.sleep(0.02)
        return frame

    async def read_value(self, reg, timeout: float = 2.0) -> int:
        info = P.resolve_register(reg)
        return self._regs.get(info.addr, 0)

    async def read_battery(self) -> dict:
        raw = self._regs[0x64]
        return {"raw": raw, "voltage": raw / 100.0, "percent": P.battery_percent(raw)}

    async def read_version(self) -> str:
        return P.version_string(self._regs[0x2E], self._regs[0x2F])

    async def read_temperature(self) -> float:
        return self._regs[0x40] * P.TEMP_SCALE

    async def read_magnetometer(self, ui_unit: bool = False) -> Tuple[float, float, float]:
        t = time.time() - self._t0
        raw = (360 + 20 * math.sin(t), 105 + 20 * math.cos(t), 122)
        div = P.MAG_UI_DIVISOR if ui_unit else 1.0
        return raw[0] / div, raw[1] / div, raw[2] / div

    async def read_quaternion(self) -> Tuple[float, float, float, float]:
        t = time.time() - self._t0
        half = math.radians((t * 30) % 360) / 2
        return 0.0, 0.0, math.sin(half), math.cos(half)

    async def read_device_info(self) -> dict:
        return {
            "battery": await self.read_battery(),
            "version": await self.read_version(),
            "temperature": await self.read_temperature(),
            "magnetometer": await self.read_magnetometer(),
        }

    # 设置类指令同样记录即可
    async def unlock(self) -> None:
        self._log("解锁")

    async def save(self) -> None:
        self._log("保存配置")

    async def restore_default(self) -> None:
        self._log("恢复默认配置")

    async def reboot(self) -> None:
        self._log("重启设备")

    async def acc_calibration(self) -> None:
        self._log("加速度校准")

    async def mag_calibration_start(self) -> None:
        self._log("进入磁场校准")

    async def mag_calibration_finish(self) -> None:
        self._log("退出磁场校准并保存")

    async def yaw_zero(self) -> None:
        self._log("Z 轴角度归零")

    async def angle_reference(self) -> None:
        self._log("设置角度参考")

    async def set_rate(self, rate: int) -> None:
        await self.write_register(0x03, rate)

    async def set_bandwidth(self, level: int) -> None:
        await self.write_register(0x1F, level)

    async def set_orientation(self, horizontal: int) -> None:
        await self.write_register(0x23, horizontal)

    async def set_algorithm(self, axis6: int) -> None:
        await self.write_register(0x24, axis6)

    async def set_output_mode(self, mode: int) -> None:
        await self.write_register(0x96, mode)
        self.mode = mode

    async def set_device_name(self, name: str) -> None:
        self._log(f"设置蓝牙名称 {name}")

    async def set_time(self, when=None) -> None:
        self._log("设置模块时间")
