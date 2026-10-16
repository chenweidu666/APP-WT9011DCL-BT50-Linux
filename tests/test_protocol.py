"""协议解析/组包单元测试：尽量用官方文档里给出的字节样例做断言。

运行：python -m unittest discover -s tests -v
"""

import struct
import unittest

from wt9011 import protocol as P


def sensor_frame(values):
    """用 9 个 int16 拼一个 0x55 0x61 数据帧。"""
    return bytes([0x55, 0x61]) + struct.pack("<9h", *values)


class TestPacketParser(unittest.TestCase):
    def test_single_frame(self):
        parser = P.PacketParser()
        frames = parser.feed(sensor_frame([1, 2, 3, 4, 5, 6, 7, 8, 9]))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0][0], 0x55)

    def test_split_across_notifications(self):
        """BLE 通知可能把一个 20 字节包拆开/粘包。"""
        raw = sensor_frame([100] * 9)
        parser = P.PacketParser()
        self.assertEqual(parser.feed(raw[:7]), [])
        self.assertEqual(parser.feed(raw[7:19]), [])
        frames = parser.feed(raw[19:] + raw)  # 补齐 + 再来一整包
        self.assertEqual(len(frames), 2)

    def test_junk_prefix_is_dropped(self):
        parser = P.PacketParser()
        frames = parser.feed(b"\x00\x11\x22" + sensor_frame([1] * 9))
        self.assertEqual(len(frames), 1)
        self.assertGreaterEqual(parser.dropped, 3)

    def test_bad_flag_resync(self):
        parser = P.PacketParser()
        frames = parser.feed(bytes([0x55, 0x99]) + sensor_frame([2] * 9))
        self.assertEqual(len(frames), 1)

    def test_garbage_between_frames(self):
        parser = P.PacketParser()
        data = sensor_frame([1] * 9) + b"\x55\x55" + sensor_frame([2] * 9)
        self.assertEqual(len(parser.feed(data)), 2)


class TestSensorParsing(unittest.TestCase):
    def test_default_mode_scaling(self):
        # 0x61 数据包：满量程 / 半量程 各轴
        frame = sensor_frame([0x2000, -0x2000, 0x7FFF, 0x0400, -0x0400, 0, 0x4000, -0x4000, 0])
        s = P.parse_sensor_frame(frame, mode=0, t=123.0)
        self.assertAlmostEqual(s.ax, 4.0, places=3)          # 0x2000/32768*16 = 4 g
        self.assertAlmostEqual(s.ay, -4.0, places=3)
        self.assertAlmostEqual(s.gx, 62.5, places=3)          # 0x0400/32768*2000 = 62.5 °/s
        self.assertAlmostEqual(s.gz, 0.0)
        self.assertAlmostEqual(s.roll, 90.0, places=3)        # 0x4000/32768*180 = 90°
        self.assertAlmostEqual(s.pitch, -90.0, places=3)
        self.assertEqual(s.t, 123.0)
        self.assertEqual(s.mode, 0)

    def test_position_mode(self):
        frame = sensor_frame([10, -20, 30, 100, -200, 300, 1000, 2000, 3000])
        s = P.parse_sensor_frame(frame, mode=1)
        self.assertEqual((s.pos_e, s.pos_n, s.pos_u), (10.0, -20.0, 30.0))
        self.assertEqual((s.vel_e, s.vel_n, s.vel_u), (100.0, -200.0, 300.0))
        self.assertAlmostEqual(s.yaw, 3000 * P.ANGLE_SCALE, places=3)

    def test_parse_rejects_wrong_frame(self):
        with self.assertRaises(ValueError):
            P.parse_sensor_frame(bytes([0x55, 0x71]) + bytes(18))


class TestRegisterParsing(unittest.TestCase):
    def test_documented_magnetometer_frame(self):
        """文档样例：55 71 3A 00 68 01 69 00 7A 00 ... → Hx=360, Hy=105, Hz=122。"""
        frame = bytes.fromhex("55713A00680169007A00") + bytes(10)
        hx, hy, hz = P.parse_mag_frame(frame)
        self.assertEqual((hx, hy, hz), (360, 105, 122))
        self.assertAlmostEqual(hx / P.MAG_UI_DIVISOR, 3.0, places=3)

    def test_register_frame_lookup(self):
        frame = bytes([0x55, 0x71, 0x34, 0x00]) + struct.pack("<8h", *range(8))
        rf = P.parse_register_frame(frame)
        self.assertEqual(rf.start_reg, 0x34)
        self.assertEqual(rf.get(0x34), 0)
        self.assertEqual(rf.get(0x37), 3)
        self.assertIsNone(rf.get(0x40))


class TestCommands(unittest.TestCase):
    def test_documented_commands(self):
        self.assertEqual(P.encode_read(0x3A).hex().upper(), "FFAA273A00")   # 读磁场
        self.assertEqual(P.encode_read(0x51).hex().upper(), "FFAA275100")   # 读四元数
        self.assertEqual(P.encode_read(0x40).hex().upper(), "FFAA274000")   # 读温度
        self.assertEqual(P.encode_read(0x64).hex().upper(), "FFAA276400")   # 读电量
        self.assertEqual(P.CMD_UNLOCK.hex().upper(), "FFAA6988B5")          # 解锁
        self.assertEqual(P.CMD_SAVE.hex().upper(), "FFAA000000")            # 保存
        self.assertEqual(P.CMD_RESTORE_DEFAULT.hex().upper(), "FFAA000100")  # 恢复默认
        self.assertEqual(P.CMD_REBOOT.hex().upper(), "FFAA00FF00")          # 重启
        self.assertEqual(P.CMD_ACC_CALIB.hex().upper(), "FFAA010100")        # 加速度校准
        self.assertEqual(P.CMD_MAG_CALIB_START.hex().upper(), "FFAA010700")   # 进入磁场校准
        self.assertEqual(P.CMD_CALIB_EXIT.hex().upper(), "FFAA010000")        # 退出校准
        self.assertEqual(P.CMD_YAW_ZERO.hex().upper(), "FFAA010400")          # Z 轴归零
        self.assertEqual(P.CMD_ANGLE_REF.hex().upper(), "FFAA010800")         # 角度参考

    def test_write_register_little_endian(self):
        self.assertEqual(P.encode_write(0x03, 0x07).hex().upper(), "FFAA030700")
        self.assertEqual(P.encode_write(0x23, 0x0001).hex().upper(), "FFAA230100")
        self.assertEqual(P.encode_write(0x96, 0x0001).hex().upper(), "FFAA960100")
        self.assertEqual(P.encode_write(0x1F, 0x0001).hex().upper(), "FFAA1F0100")


class TestHelpers(unittest.TestCase):
    def test_resolve_register(self):
        self.assertEqual(P.resolve_register("RATE").addr, 0x03)
        self.assertEqual(P.resolve_register("rate").addr, 0x03)
        self.assertEqual(P.resolve_register("0x3A").addr, 0x3A)
        self.assertEqual(P.resolve_register(0x96).name, "AGPVSEL")
        with self.assertRaises(KeyError):
            P.resolve_register("NOT_A_REG")

    def test_battery_percent(self):
        self.assertEqual(P.battery_percent(420), 100)
        self.assertEqual(P.battery_percent(414), 100)
        self.assertEqual(P.battery_percent(410), 95)
        self.assertEqual(P.battery_percent(343), 5)
        self.assertEqual(P.battery_percent(300), 0)

    def test_version_string_matches_doc_example(self):
        """文档样例：Version1L=03 Version1H=01 Version2L=D8 Version2H=89 → 10080.1.3"""
        self.assertEqual(P.version_string(0x0103, 0x89D8), "10080.1.3")

    def test_signed_conversion(self):
        self.assertEqual(P.to_signed16(0xFFFF), -1)
        self.assertEqual(P.to_signed16(0x8000), -32768)
        self.assertEqual(P.to_signed16(0x7FFF), 32767)

    def test_scaled_register_values(self):
        self.assertAlmostEqual(P.register_scaled(0x34, 0x2000), 4.0, places=4)   # 加速度 g
        self.assertAlmostEqual(P.register_scaled(0x40, 2650), 26.5, places=4)    # 温度 ℃
        self.assertIsNone(P.register_scaled(0x64, 400))                          # 电量无换算系数


if __name__ == "__main__":
    unittest.main()
