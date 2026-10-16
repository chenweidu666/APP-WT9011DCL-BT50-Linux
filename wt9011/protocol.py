"""WT9011DCL-BT50（维特智能 BLE 5.0 协议）数据包解析与指令组包。

协议来源（已镜像到 WT9011DCL-BT50-资料/）：
  - 《蓝牙5.0通讯协议》  https://wit-motion.yuque.com/wumwnr/docs/ycui87fgg1mepk1u
  - 官方 Python SDK      https://github.com/WITMOTION/WitBluetooth_BWT901BLE5_0/tree/main/Python
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field, asdict
from typing import Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- BLE GATT 常量
SERVICE_UUID = "0000ffe5-0000-1000-8000-00805f9a34fb"
NOTIFY_UUID = "0000ffe4-0000-1000-8000-00805f9a34fb"
WRITE_UUID = "0000ffe9-0000-1000-8000-00805f9a34fb"

# 设备名以 WT / BWT 开头（上位机也靠这个过滤无关蓝牙设备）
NAME_PREFIXES = ("WT", "BWT")

HEADER = 0x55
FLAG_SENSOR = 0x61  # 传感器数据包（加速度/角速度/角度 或 位移/位移速度/角度）
FLAG_REGISTER = 0x71  # 寄存器回传包
PACKET_LEN = 20

# ---------------------------------------------------------------- 物理量换算
ACC_SCALE = 16.0 / 32768.0  # g
GYRO_SCALE = 2000.0 / 32768.0  # °/s
ANGLE_SCALE = 180.0 / 32768.0  # °
QUAT_SCALE = 1.0 / 32768.0
TEMP_SCALE = 0.01  # ℃
# 磁场：寄存器原始值单位 mG；官方 SDK 用 raw/120 得到与上位机一致的显示值
MAG_UI_DIVISOR = 120.0


def to_signed16(value: int) -> int:
    """把 0..65535 的整数解释为有符号 int16。"""
    value &= 0xFFFF
    return value - 0x10000 if value >= 0x8000 else value


# ---------------------------------------------------------------- 数据模型
@dataclass
class Sample:
    """一帧解析后的传感器数据。

    mode=0（默认，AGPVSEL=0）：加速度 + 角速度 + 角度
    mode=1（AGPVSEL=1）：位移 + 位移速度 + 角度
    """

    t: float = 0.0  # 本地接收时间戳（秒，time.time()）
    mode: int = 0
    ax: float = 0.0
    ay: float = 0.0
    az: float = 0.0
    gx: float = 0.0
    gy: float = 0.0
    gz: float = 0.0
    pos_e: float = 0.0  # mm
    pos_n: float = 0.0
    pos_u: float = 0.0
    vel_e: float = 0.0  # mm/s
    vel_n: float = 0.0
    vel_u: float = 0.0
    roll: float = 0.0  # °X
    pitch: float = 0.0  # °Y
    yaw: float = 0.0  # °Z

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RegisterFrame:
    """0x55 0x71 寄存器回传包：起始寄存器地址 + 8 个寄存器值。"""

    start_reg: int
    values: List[int] = field(default_factory=list)

    def get(self, reg: int) -> Optional[int]:
        idx = reg - self.start_reg
        if 0 <= idx < len(self.values):
            return self.values[idx]
        return None


class PacketParser:
    """字节流 → 定长 20 字节帧的增量解析器（与官方 SDK 行为一致）。"""

    def __init__(self) -> None:
        self._buf: bytearray = bytearray()
        self.dropped = 0

    def feed(self, data: bytes) -> List[bytes]:
        """喂入一段 BLE 通知数据，返回其中完整且合法的 20 字节帧列表。"""
        self._buf.extend(data)
        frames: List[bytes] = []
        while True:
            if len(self._buf) < 2:
                break
            if self._buf[0] != HEADER:
                del self._buf[0]
                self.dropped += 1
                continue
            if self._buf[1] not in (FLAG_SENSOR, FLAG_REGISTER):
                del self._buf[0]
                self.dropped += 1
                continue
            if len(self._buf) < PACKET_LEN:
                break
            frames.append(bytes(self._buf[:PACKET_LEN]))
            del self._buf[:PACKET_LEN]
        return frames

    def reset(self) -> None:
        self._buf.clear()


def parse_sensor_frame(frame: bytes, mode: int = 0, t: float = 0.0) -> Sample:
    """解析 0x55 0x61 数据包（20 字节，9 个 int16 小端）。"""
    if len(frame) != PACKET_LEN or frame[0] != HEADER or frame[1] != FLAG_SENSOR:
        raise ValueError("不是合法的 0x55 0x61 数据帧")
    raw = struct.unpack("<9h", frame[2:20])
    s = Sample(t=t, mode=mode)
    if mode == 1:
        s.pos_e, s.pos_n, s.pos_u = (float(v) for v in raw[0:3])  # mm
        s.vel_e, s.vel_n, s.vel_u = (float(v) for v in raw[3:6])  # mm/s
    else:
        s.ax = raw[0] * ACC_SCALE
        s.ay = raw[1] * ACC_SCALE
        s.az = raw[2] * ACC_SCALE
        s.gx = raw[3] * GYRO_SCALE
        s.gy = raw[4] * GYRO_SCALE
        s.gz = raw[5] * GYRO_SCALE
    s.roll = raw[6] * ANGLE_SCALE
    s.pitch = raw[7] * ANGLE_SCALE
    s.yaw = raw[8] * ANGLE_SCALE
    return s


def parse_register_frame(frame: bytes) -> RegisterFrame:
    """解析 0x55 0x71 寄存器回传包（20 字节：头 2 + 起始寄存器 2 + 8×int16）。"""
    if len(frame) != PACKET_LEN or frame[0] != HEADER or frame[1] != FLAG_REGISTER:
        raise ValueError("不是合法的 0x55 0x71 寄存器帧")
    start_reg = struct.unpack("<H", frame[2:4])[0]
    values = list(struct.unpack("<8h", frame[4:20]))
    return RegisterFrame(start_reg=start_reg, values=values)


# ---------------------------------------------------------------- 指令组包
def encode_read(reg: int) -> bytes:
    """读寄存器：FF AA 27 XX 00"""
    return bytes([0xFF, 0xAA, 0x27, reg & 0xFF, 0x00])


def encode_write(reg: int, value: int) -> bytes:
    """写寄存器：FF AA XX VALL VALH（低字节在前）。"""
    value &= 0xFFFF
    return bytes([0xFF, 0xAA, reg & 0xFF, value & 0xFF, (value >> 8) & 0xFF])


# 常用固定指令
CMD_UNLOCK = encode_write(0x69, 0xB588)  # FF AA 69 88 B5 解锁（10 秒内有效）
CMD_SAVE = encode_write(0x00, 0x0000)  # FF AA 00 00 00 保存当前配置
CMD_RESTORE_DEFAULT = encode_write(0x00, 0x0001)  # 恢复默认配置并保存
CMD_REBOOT = encode_write(0x00, 0x00FF)  # FF AA 00 FF 00 重启
CMD_ACC_CALIB = encode_write(0x01, 0x0001)  # 加速度校准
CMD_MAG_CALIB_START = encode_write(0x01, 0x0007)  # 进入磁场校准
CMD_CALIB_EXIT = encode_write(0x01, 0x0000)  # 退出校准
CMD_YAW_ZERO = encode_write(0x01, 0x0004)  # Z 轴角度归零（需先切六轴算法）
CMD_ANGLE_REF = encode_write(0x01, 0x0008)  # 设置角度参考

# ---------------------------------------------------------------- 寄存器表
@dataclass(frozen=True)
class RegisterInfo:
    addr: int
    name: str
    desc: str
    rw: str = "R/W"
    options: Tuple[Tuple[int, str], ...] = ()
    scale: Optional[float] = None  # 原始值 × scale = 物理量
    unit: str = ""

    @property
    def label(self) -> str:
        return f"0x{self.addr:02X}  {self.name}"


REGISTERS: Tuple[RegisterInfo, ...] = (
    RegisterInfo(0x00, "SAVE", "保存配置：0=保存当前配置, 1=恢复默认并保存, 0xFF=重启", "W"),
    RegisterInfo(0x01, "CALSW", "校准开关：0x0001 加速度校准, 0x0007 磁场校准, 0x0000 退出, 0x0004 Z轴归零, 0x0008 角度参考", "W"),
    RegisterInfo(0x03, "RATE", "回传速率", "R/W", (
        (0x01, "0.1Hz"), (0x02, "0.5Hz"), (0x03, "1Hz"), (0x04, "2Hz"), (0x05, "5Hz"),
        (0x06, "10Hz（默认）"), (0x07, "20Hz"), (0x08, "50Hz"), (0x09, "100Hz"), (0x0B, "200Hz"),
    )),
    RegisterInfo(0x04, "BAUD", "串口波特率编码", "R/W"),
    RegisterInfo(0x05, "AXOFFSET", "X 轴加速度零偏", "R/W", scale=ACC_SCALE, unit="g"),
    RegisterInfo(0x06, "AYOFFSET", "Y 轴加速度零偏", "R/W", scale=ACC_SCALE, unit="g"),
    RegisterInfo(0x07, "AZOFFSET", "Z 轴加速度零偏", "R/W", scale=ACC_SCALE, unit="g"),
    RegisterInfo(0x08, "GXOFFSET", "X 轴角速度零偏", "R/W", scale=GYRO_SCALE, unit="°/s"),
    RegisterInfo(0x09, "GYOFFSET", "Y 轴角速度零偏", "R/W", scale=GYRO_SCALE, unit="°/s"),
    RegisterInfo(0x0A, "GZOFFSET", "Z 轴角速度零偏", "R/W", scale=GYRO_SCALE, unit="°/s"),
    RegisterInfo(0x0B, "HXOFFSET", "X 轴磁场零偏", "R/W"),
    RegisterInfo(0x0C, "HYOFFSET", "Y 轴磁场零偏", "R/W"),
    RegisterInfo(0x0D, "HZOFFSET", "Z 轴磁场零偏", "R/W"),
    RegisterInfo(0x0E, "D0MODE", "D0 引脚模式", "R/W"),
    RegisterInfo(0x0F, "D1MODE", "D1 引脚模式", "R/W"),
    RegisterInfo(0x10, "D2MODE", "D2 引脚模式", "R/W"),
    RegisterInfo(0x11, "D3MODE", "D3 引脚模式", "R/W"),
    RegisterInfo(0x1F, "BANDWIDTH", "带宽", "R/W", (
        (0, "256Hz"), (1, "188Hz"), (2, "98Hz"), (3, "42Hz"), (4, "20Hz（默认）"), (5, "10Hz"), (6, "5Hz"),
    )),
    RegisterInfo(0x23, "ORIENT", "安装方向", "R/W", ((0, "水平安装"), (1, "垂直安装（Y 轴箭头朝上）"))),
    RegisterInfo(0x24, "AXIS6", "算法", "R/W", ((0, "9 轴（磁力解算，绝对航向）"), (1, "6 轴（积分航向，相对航向）"))),
    RegisterInfo(0x2E, "VERSION1", "版本号", "R"),
    RegisterInfo(0x2F, "VERSION2", "版本号分支/硬件版本", "R"),
    RegisterInfo(0x30, "YYMM", "年、月（BCD）", "R/W"),
    RegisterInfo(0x31, "DDHH", "日、时（BCD）", "R/W"),
    RegisterInfo(0x32, "MMSS", "分、秒（BCD）", "R/W"),
    RegisterInfo(0x33, "MS", "毫秒", "R/W"),
    RegisterInfo(0x34, "AX", "X 轴加速度", "R", scale=ACC_SCALE, unit="g"),
    RegisterInfo(0x35, "AY", "Y 轴加速度", "R", scale=ACC_SCALE, unit="g"),
    RegisterInfo(0x36, "AZ", "Z 轴加速度", "R", scale=ACC_SCALE, unit="g"),
    RegisterInfo(0x37, "GX", "X 轴角速度", "R", scale=GYRO_SCALE, unit="°/s"),
    RegisterInfo(0x38, "GY", "Y 轴角速度", "R", scale=GYRO_SCALE, unit="°/s"),
    RegisterInfo(0x39, "GZ", "Z 轴角速度", "R", scale=GYRO_SCALE, unit="°/s"),
    RegisterInfo(0x3A, "HX", "X 轴磁场（原始值单位 mG，显示值 = 原始值/120）", "R"),
    RegisterInfo(0x3B, "HY", "Y 轴磁场", "R"),
    RegisterInfo(0x3C, "HZ", "Z 轴磁场", "R"),
    RegisterInfo(0x3D, "Roll", "X 轴角度", "R", scale=ANGLE_SCALE, unit="°"),
    RegisterInfo(0x3E, "Pitch", "Y 轴角度", "R", scale=ANGLE_SCALE, unit="°"),
    RegisterInfo(0x3F, "Yaw", "Z 轴角度", "R", scale=ANGLE_SCALE, unit="°"),
    RegisterInfo(0x40, "TEMP", "模块温度", "R", scale=TEMP_SCALE, unit="℃"),
    RegisterInfo(0x51, "Q0", "四元数 Q0", "R", scale=QUAT_SCALE),
    RegisterInfo(0x52, "Q1", "四元数 Q1", "R", scale=QUAT_SCALE),
    RegisterInfo(0x53, "Q2", "四元数 Q2", "R", scale=QUAT_SCALE),
    RegisterInfo(0x54, "Q3", "四元数 Q3", "R", scale=QUAT_SCALE),
    RegisterInfo(0x64, "BATTERY", "电量原始值（raw/100 = 电压 V）", "R"),
    RegisterInfo(0x66, "MAC0", "MAC 地址 0", "R"),
    RegisterInfo(0x67, "MAC1", "MAC 地址 1", "R"),
    RegisterInfo(0x68, "MAC2", "MAC 地址 2", "R"),
    RegisterInfo(0x69, "UNLOCK", "解锁寄存器（写 0xB588）", "W"),
    RegisterInfo(0x96, "AGPVSEL", "55 61 数据包输出内容", "R/W", (
        (0, "加速度 + 角速度 + 角度（默认）"), (1, "位移 + 位移速度 + 角度"),
    )),
)

REG_BY_NAME = {r.name.upper(): r for r in REGISTERS}
REG_BY_ADDR = {r.addr: r for r in REGISTERS}


def resolve_register(token) -> RegisterInfo:
    """支持 'RATE' / '0x03' / '3' 三种写法。"""
    if isinstance(token, int):
        if token in REG_BY_ADDR:
            return REG_BY_ADDR[token]
        raise KeyError(f"未在寄存器表中登记地址 0x{token:02X}")
    key = str(token).strip()
    if key.upper() in REG_BY_NAME:
        return REG_BY_NAME[key.upper()]
    try:
        addr = int(key, 16) if key.lower().startswith("0x") else int(key)
    except ValueError:
        raise KeyError(f"无法识别的寄存器：{token}") from None
    return resolve_register(addr)


def register_scaled(reg: int, raw: int) -> Optional[float]:
    """按寄存器表把原始值换算成物理量（未登记或无换算系数时返回 None）。"""
    info = REG_BY_ADDR.get(reg)
    if not info or info.scale is None:
        return None
    return raw * info.scale


# ---------------------------------------------------------------- 电量/版本换算
def battery_percent(raw: int) -> int:
    """按官方电量曲线（寄存器值≈mV）估算电量百分比。"""
    table = [
        (413, 100), (406, 95), (402, 90), (397, 85), (393, 80), (390, 75), (386, 70),
        (384, 65), (381, 60), (376, 55), (373, 50), (370, 45), (368, 40), (366, 35),
        (364, 30), (362, 25), (359, 20), (355, 15), (350, 10), (342, 5),
    ]
    for thr, pct in table:
        if raw > thr:
            return pct
    return 0


def version_string(version1_raw: int, version2_raw: int) -> str:
    """按协议：PRO_VERSION.HW_VERSION.SW_VERSION，例 10080.1.3"""
    v1 = version1_raw & 0xFFFF
    v2 = version2_raw & 0xFFFF
    version1_l = v1 & 0xFF
    version1_h = (v1 >> 8) & 0xFF
    version2_l = v2 & 0xFF
    version2_h = (v2 >> 8) & 0xFF
    pro = (((version2_h & 0x7F) << 8) | version2_l) << 2 | (version1_h >> 6)
    hw = version1_h & 0x3F
    sw = version1_l
    return f"{pro}.{hw}.{sw}"


def format_mag(raw: int) -> Tuple[int, float]:
    """返回（原始 mG, 上位机口径显示值 raw/120）。"""
    return raw, raw / MAG_UI_DIVISOR


def parse_mag_frame(frame: bytes) -> Tuple[int, int, int]:
    """0x55 0x71 0x3A 磁场帧的便捷解析（返回原始 mG）。"""
    rf = parse_register_frame(frame)
    if rf.start_reg != 0x3A:
        raise ValueError(f"起始寄存器不是 0x3A：0x{rf.start_reg:02X}")
    return rf.values[0], rf.values[1], rf.values[2]


def bcd(value: int) -> str:
    """寄存器里的日期/时间是 BCD 编码。"""
    return f"{(value >> 4) & 0xF}{value & 0xF}"
