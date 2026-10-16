"""命令行工具：无界面时的扫描 / 读数 / 记录 / 配置 / 校准。

用法示例：
    python -m wt9011.cli scan
    python -m wt9011.cli stream --csv record.csv --seconds 10
    python -m wt9011.cli info
    python -m wt9011.cli read RATE
    python -m wt9011.cli write RATE 0x07
    python -m wt9011.cli calib mag-start
    python -m wt9011.cli stream --mock          # 无硬件演示
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from typing import List, Optional

from . import protocol as P
from .client import WitClient, WitError
from .sim import SimulatedWitClient

CSV_FIELDS = ["t", "mode", "ax", "ay", "az", "gx", "gy", "gz",
              "pos_e", "pos_n", "pos_u", "vel_e", "vel_n", "vel_u",
              "roll", "pitch", "yaw"]


def build_client(args) -> object:
    def status(msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr)

    if args.mock:
        return SimulatedWitClient(on_status=status)
    return WitClient(address=args.addr, on_status=status)


async def _connect(client, args):
    await client.connect(args.addr if not args.mock else None, scan_timeout=args.scan_timeout)


async def cmd_scan(args) -> int:
    if args.mock:
        print("模拟模式：假设备 MOCK:00:11:22:33:44:55")
        return 0
    devices = await WitClient.scan(timeout=args.scan_timeout)
    if not devices:
        print("未发现维特设备（名字以 WT/BWT 开头，或广播带 FFE5 服务）")
        return 1
    print(f"{'地址':<20} {'RSSI':>5}  名称")
    for dev, adv, rssi in devices:
        print(f"{dev.address:<20} {rssi:>5}  {dev.name or adv.local_name or '(无名)'}")
    return 0


async def cmd_info(args) -> int:
    client = build_client(args)
    await _connect(client, args)
    try:
        info = await client.read_device_info()
        bat = info.get("battery")
        if bat:
            print(f"电量    : {bat['percent']}%  ({bat['voltage']:.2f} V, raw={bat['raw']})")
        print(f"版本号  : {info.get('version', '读取失败')}")
        print(f"温度    : {info.get('temperature', '读取失败')} ℃")
        mag = info.get("magnetometer")
        if mag:
            print(f"磁场    : {mag[0]:.1f}, {mag[1]:.1f}, {mag[2]:.1f} (mG 原始值)")
        for key in ("battery_error", "version_error", "temperature_error", "magnetometer_error"):
            if key in info:
                print(f"  ! {key}: {info[key]}")
    finally:
        await client.disconnect()
    return 0


async def cmd_stream(args) -> int:
    client = build_client(args)
    if args.rate:
        await _connect(client, args)
    else:
        await _connect(client, args)
    try:
        if args.rate:
            await client.set_rate(args.rate)
            print(f"已设置回传速率 {args.rate}", file=sys.stderr)
        writer = None
        fh = None
        if args.csv:
            fh = open(args.csv, "w", newline="")
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            writer.writeheader()
            print(f"记录到 {args.csv}", file=sys.stderr)
        t0 = time.time()
        last = 0.0
        try:
            async for s in client.stream():
                if args.csv:
                    row = s.as_dict()
                    writer.writerow({k: row.get(k) for k in CSV_FIELDS})
                now = time.time()
                if now - last >= 1.0 / max(args.print_rate, 0.5):
                    last = now
                    print(f"{s.t - t0:8.3f}s  "
                          f"acc=({s.ax:6.2f},{s.ay:6.2f},{s.az:6.2f})g  "
                          f"gyro=({s.gx:7.1f},{s.gy:7.1f},{s.gz:7.1f})°/s  "
                          f"角=({s.roll:7.2f},{s.pitch:7.2f},{s.yaw:7.2f})°  "
                          f"[{client.sample_rate:5.1f} Hz]")
                if args.seconds and now - t0 >= args.seconds:
                    break
        finally:
            if fh:
                fh.close()
    finally:
        await client.disconnect()
    return 0


async def cmd_read(args) -> int:
    client = build_client(args)
    await _connect(client, args)
    try:
        info = P.resolve_register(args.reg)
        rf = await client.read_registers(info.addr)
        print(f"起始寄存器 0x{rf.start_reg:02X}（{P.REG_BY_ADDR.get(rf.start_reg, None).name if rf.start_reg in P.REG_BY_ADDR else '?'}）")
        for i, value in enumerate(rf.values):
            addr = rf.start_reg + i
            meta = P.REG_BY_ADDR.get(addr)
            scaled = P.register_scaled(addr, value)
            name = meta.name if meta else "?"
            unit = meta.unit if meta else ""
            extra = f"  → {scaled:.4f} {unit}" if scaled is not None else ""
            print(f"  0x{addr:02X}  {name:<10} = {value:6d} (0x{value & 0xFFFF:04X}){extra}")
    finally:
        await client.disconnect()
    return 0


async def cmd_write(args) -> int:
    client = build_client(args)
    await _connect(client, args)
    try:
        info = P.resolve_register(args.reg)
        print(f"写 {info.name} (0x{info.addr:02X}) = {args.value}  保存={not args.no_save}")
        await client.write_register(info.addr, args.value, save=not args.no_save)
        if args.verify:
            rf = await client.read_registers(info.addr)
            print(f"回读 = {rf.get(info.addr)}")
    finally:
        await client.disconnect()
    return 0


async def cmd_calib(args) -> int:
    client = build_client(args)
    await _connect(client, args)
    try:
        if args.what == "acc":
            print("加速度校准：请保持模块水平静止 3 秒…")
            await client.acc_calibration()
            print("已下发加速度校准指令")
        elif args.what == "mag-start":
            await client.mag_calibration_start()
            print("已进入磁场校准，请绕 X/Y/Z 三轴各缓慢转 3 圈，然后执行 calib mag-stop")
        elif args.what == "mag-stop":
            await client.mag_calibration_finish()
            print("已退出磁场校准并保存")
        elif args.what == "yaw-zero":
            print("Z 轴角度归零（需模块当前为六轴算法）")
            await client.yaw_zero()
        elif args.what == "angle-ref":
            await client.angle_reference()
            print("已把当前姿态设为角度参考")
    finally:
        await client.disconnect()
    return 0


async def cmd_mode(args) -> int:
    client = build_client(args)
    await _connect(client, args)
    try:
        await client.set_output_mode(args.mode)
        print(f"数据包输出内容已设置为 {'位移+位移速度+角度' if args.mode else '加速度+角速度+角度'}")
    finally:
        await client.disconnect()
    return 0


async def cmd_send(args) -> int:
    client = build_client(args)
    await _connect(client, args)
    try:
        payload = bytes.fromhex(args.hex.replace(" ", ""))
        print(f"发送 {payload.hex(' ').upper()}")
        await client.send(payload)
    finally:
        await client.disconnect()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="wt9011", description="WT9011DCL-BT50 Linux 客户端（BLE 5.0）")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--addr", help="设备 MAC 地址（省略则自动扫描 WT/BWT 设备）")
    common.add_argument("--scan-timeout", type=float, default=10.0, help="扫描超时（秒）")
    common.add_argument("--mock", action="store_true", help="不开硬件，用模拟数据")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", parents=[common], help="扫描附近的维特设备")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("info", parents=[common], help="读取电量/版本/温度/磁场")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("stream", parents=[common], help="实时打印数据（可记录 CSV）")
    p.add_argument("--csv", help="保存 CSV 文件名")
    p.add_argument("--seconds", type=float, default=0, help="采集时长（秒），0=一直采")
    p.add_argument("--print-rate", type=float, default=5.0, help="终端打印频率 Hz")
    p.add_argument("--rate", type=int, help="先设置回传速率（0x01..0x0B）")
    p.set_defaults(func=cmd_stream)

    p = sub.add_parser("read", parents=[common], help="读取寄存器（返回 8 个）")
    p.add_argument("reg", help="寄存器名或地址，如 RATE / 0x03 / 3")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("write", parents=[common], help="写寄存器（自动解锁+保存）")
    p.add_argument("reg")
    p.add_argument("value", type=lambda v: int(v, 0), help="十进制或 0x 十六进制")
    p.add_argument("--no-save", action="store_true", help="不发送保存指令")
    p.add_argument("--verify", action="store_true", help="写完回读一次")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("calib", parents=[common], help="校准/归零")
    p.add_argument("what", choices=["acc", "mag-start", "mag-stop", "yaw-zero", "angle-ref"])
    p.set_defaults(func=cmd_calib)

    p = sub.add_parser("mode", parents=[common], help="55 61 数据包输出内容")
    p.add_argument("mode", type=int, choices=[0, 1], help="0=加速度/角速度/角度 1=位移/位移速度/角度")
    p.set_defaults(func=cmd_mode)

    p = sub.add_parser("send", parents=[common], help="直接发送十六进制指令（高级）")
    p.add_argument("hex", help="例如 'FF AA 27 3A 00'")
    p.set_defaults(func=cmd_send)

    args = parser.parse_args(argv)
    try:
        return asyncio.run(args.func(args))
    except WitError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
