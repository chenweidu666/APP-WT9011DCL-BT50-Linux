"""Linux 桌面上位机（PySide6 + pyqtgraph）。

对照 Windows 版 WitMotion 上位机的常用功能：
  - 扫描/连接 BLE 5.0 维特设备（WT/BWT 开头）
  - 实时数值 + 曲线（加速度/角速度/角度/磁场/四元数/位移）
  - 姿态仪（roll/pitch）
  - 回传速率、带宽、算法、安装方向、输出内容等配置
  - 加速度校准、磁场校准向导、Z 轴归零、角度参考、保存/恢复默认/重启
  - 数据记录为 CSV

启动：python -m wt9011.gui   （加 --mock 可用模拟数据演示）
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
import time
from collections import deque
from typing import Dict, List, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

import pyqtgraph as pg

from . import protocol as P
from .client import WitClient
from .sim import SimulatedWitClient

CSV_FIELDS = ["t", "mode", "ax", "ay", "az", "gx", "gy", "gz",
              "pos_e", "pos_n", "pos_u", "vel_e", "vel_n", "vel_u",
              "roll", "pitch", "yaw", "mx", "my", "mz", "q0", "q1", "q2", "q3"]

CHANNELS = {
    "加速度 (g)": [("ax", "AX", "#e05252"), ("ay", "AY", "#4caf50"), ("az", "AZ", "#4a90e2")],
    "角速度 (°/s)": [("gx", "GX", "#e05252"), ("gy", "GY", "#4caf50"), ("gz", "GZ", "#4a90e2")],
    "角度 (°)": [("roll", "Roll", "#e05252"), ("pitch", "Pitch", "#4caf50"), ("yaw", "Yaw", "#4a90e2")],
    "位移 (mm)": [("pos_e", "PosE", "#e05252"), ("pos_n", "PosN", "#4caf50"), ("pos_u", "PosU", "#4a90e2")],
    "位移速度 (mm/s)": [("vel_e", "VelE", "#e05252"), ("vel_n", "VelN", "#4caf50"), ("vel_u", "VelU", "#4a90e2")],
    "磁场 (mG)": [("mx", "HX", "#e05252"), ("my", "HY", "#4caf50"), ("mz", "HZ", "#4a90e2")],
    "四元数": [("q0", "Q0", "#e05252"), ("q1", "Q1", "#4caf50"), ("q2", "Q2", "#4a90e2"), ("q3", "Q3", "#9b59b6")],
}

RATE_OPTIONS = [(0x01, "0.1 Hz"), (0x02, "0.5 Hz"), (0x03, "1 Hz"), (0x04, "2 Hz"), (0x05, "5 Hz"),
                (0x06, "10 Hz（默认）"), (0x07, "20 Hz"), (0x08, "50 Hz"), (0x09, "100 Hz"), (0x0B, "200 Hz")]
BW_OPTIONS = [(0, "256 Hz"), (1, "188 Hz"), (2, "98 Hz"), (3, "42 Hz"), (4, "20 Hz（默认）"), (5, "10 Hz"), (6, "5 Hz")]


# --------------------------------------------------------------------- BLE 线程
class BleWorker(QThread):
    """在独立线程里跑 asyncio 事件循环，负责所有 BLE 交互。"""

    sampleReady = Signal(object)
    statusMsg = Signal(str)
    scanResult = Signal(object)
    infoReady = Signal(dict)
    auxReady = Signal(dict)
    connChanged = Signal(bool)
    errorMsg = Signal(str)

    def __init__(self, mock: bool = False, address: Optional[str] = None, parent=None) -> None:
        super().__init__(parent)
        self.mock = mock
        self.address = address
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.client = None

    # ---- 线程主体
    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def shutdown(self) -> None:
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.wait(2000)

    # ---- 客户端与回调
    def _ensure_client(self):
        if self.client is None:
            if self.mock:
                self.client = SimulatedWitClient(on_sample=self.sampleReady.emit,
                                                 on_status=self.statusMsg.emit)
            else:
                self.client = WitClient(address=self.address,
                                        on_sample=self.sampleReady.emit,
                                        on_status=self.statusMsg.emit)
        return self.client

    def submit(self, coro_factory) -> None:
        """把协程丢到事件循环里执行（coro_factory 在工作线程内调用）。"""
        if not self.loop:
            self.errorMsg.emit("内部事件循环未就绪")
            return

        async def runner():
            try:
                await coro_factory()
            except Exception as exc:  # noqa: BLE001
                self.errorMsg.emit(f"{type(exc).__name__}: {exc}")

        asyncio.run_coroutine_threadsafe(runner(), self.loop)

    # ---- 具体动作
    def do_scan(self, timeout: float = 8.0) -> None:
        async def job():
            if self.mock:
                self.scanResult.emit([("MOCK:00:11:22:33:44:55", "模拟设备", -40)])
                return
            found = await WitClient.scan(timeout=timeout)
            self.scanResult.emit([(d.address, d.name or "", rssi) for d, _adv, rssi in found])

        self.submit(job)

    def do_connect(self, address: Optional[str] = None, scan_timeout: float = 10.0) -> None:
        async def job():
            client = self._ensure_client()
            await client.connect(address or self.address, scan_timeout=scan_timeout)
            self.connChanged.emit(True)
            await self._read_info(client)

        self.submit(job)

    def do_disconnect(self) -> None:
        async def job():
            if self.client:
                await self.client.disconnect()
            self.connChanged.emit(False)

        self.submit(job)

    async def _read_info(self, client) -> None:
        self.infoReady.emit(await client.read_device_info())

    def do_read_info(self) -> None:
        self.submit(lambda: self._read_info(self.client))

    def do_poll_aux(self, include_device: bool = False) -> None:
        """轮询磁场/四元数（可选电量、温度）。"""
        async def job():
            client = self.client
            if not client or not client.is_connected:
                return
            aux: dict = {}
            try:
                mag = await client.read_magnetometer()
                aux["magnetometer"] = mag
            except Exception:
                pass
            try:
                aux["quaternion"] = await client.read_quaternion()
            except Exception:
                pass
            if include_device:
                try:
                    aux["battery"] = await client.read_battery()
                except Exception:
                    pass
                try:
                    aux["temperature"] = await client.read_temperature()
                except Exception:
                    pass
            if aux:
                self.auxReady.emit(aux)

        self.submit(job)

    def do_call(self, method: str, *args, success: Optional[str] = None, **kwargs) -> None:
        """执行客户端的任意协程方法。"""
        async def job():
            client = self._ensure_client()
            await getattr(client, method)(*args, **kwargs)
            if success:
                self.statusMsg.emit(success)

        self.submit(job)


# --------------------------------------------------------------------- 姿态仪
class AttitudeIndicator(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.roll = 0.0
        self.pitch = 0.0
        self.setMinimumHeight(160)

    def set_angles(self, roll: float, pitch: float) -> None:
        self.roll, self.pitch = roll, pitch
        self.update()

    def paintEvent(self, _event) -> None:
        w, h = self.width(), self.height()
        cx, cy, r = w / 2, h / 2, min(w, h) / 2 - 6
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # 圆形裁剪区域
        clip = QPainterPath()
        clip.addEllipse(cx - r, cy - r, 2 * r, 2 * r)
        p.setClipPath(clip)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor("#1b2631")))
        p.drawEllipse(QPointF(cx, cy), r, r)

        # 天/地（随 roll 旋转、随 pitch 平移）
        p.save()
        p.translate(cx, cy)
        p.rotate(-self.roll)
        offset = self.pitch * r / 45.0
        p.setBrush(QBrush(QColor("#1f6f8b")))
        p.drawRect(QRectF(-2 * r, -2 * r + offset, 4 * r, 2 * r))
        p.setBrush(QBrush(QColor("#6b4423")))
        p.drawRect(QRectF(-2 * r, offset, 4 * r, 2 * r))
        p.setPen(QPen(QColor("#ffffff"), 1.5))
        p.drawLine(QPointF(-r, offset), QPointF(r, offset))
        p.setPen(QPen(QColor("#dddddd"), 1))
        for deg in (-30, -20, -10, 10, 20, 30):
            y = offset + deg * r / 45.0
            half = r * (0.45 if deg % 20 == 0 else 0.25)
            p.drawLine(QPointF(-half, y), QPointF(half, y))
        p.restore()

        # 固定机翼符号 + 外框
        p.setClipping(False)
        p.setPen(QPen(QColor("#ffcc00"), 2.5))
        p.drawLine(QPointF(cx - r * 0.55, cy), QPointF(cx - r * 0.15, cy))
        p.drawLine(QPointF(cx + r * 0.15, cy), QPointF(cx + r * 0.55, cy))
        p.setPen(QPen(QColor("#ffffff"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), r, r)

        p.setPen(QColor("#eeeeee"))
        p.setFont(QFont("", 9))
        p.drawText(int(cx - r), int(cy + r + 16), f"Roll {self.roll:6.2f}°   Pitch {self.pitch:6.2f}°")
        p.end()


# --------------------------------------------------------------------- 主窗口
class MainWindow(QMainWindow):
    def __init__(self, mock: bool = False, address: Optional[str] = None) -> None:
        super().__init__()
        self.setWindowTitle("WT9011DCL-BT50 上位机（Linux / BLE 5.0）" + ("  [模拟模式]" if mock else ""))
        self.resize(1280, 780)

        self.worker = BleWorker(mock=mock, address=address)
        self.worker.start()
        self.samples_seen = 0
        self.last_sample: Optional[P.Sample] = None
        self.aux: dict = {}
        self.window_seconds = 10.0
        self.buffers: Dict[str, deque] = {k: deque(maxlen=6000) for ch in CHANNELS.values() for k, _n, _c in ch}
        self.curves: Dict[str, pg.PlotDataItem] = {}
        self.record_fh = None
        self.record_writer = None
        self.record_rows: List[dict] = []
        self.t0 = time.time()

        self._build_ui()
        self._connect_signals()

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._refresh)
        self.ui_timer.start(50)
        self.aux_timer = QTimer(self)
        self.aux_timer.timeout.connect(self._poll_aux)
        self.aux_timer.start(1000)

    # ---------------- UI
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        # 顶部：连接
        top = QHBoxLayout()
        self.btn_scan = QPushButton("扫描设备")
        self.cmb_dev = QComboBox()
        self.cmb_dev.setMinimumWidth(320)
        self.btn_connect = QPushButton("连接")
        self.btn_disconnect = QPushButton("断开")
        self.btn_disconnect.setEnabled(False)
        self.btn_info = QPushButton("读取设备信息")
        self.btn_info.setEnabled(False)
        for w in (self.btn_scan, self.cmb_dev, self.btn_connect, self.btn_disconnect, self.btn_info):
            top.addWidget(w)
        top.addStretch(1)
        self.lbl_state = QLabel("未连接")
        self.lbl_state.setStyleSheet("color:#888888")
        top.addWidget(self.lbl_state)
        outer.addLayout(top)

        body = QHBoxLayout()
        outer.addLayout(body, 1)

        # 左列：数值 + 姿态仪
        left = QVBoxLayout()
        body.addLayout(left, 0)

        grp_info = QGroupBox("设备信息")
        form = QFormLayout(grp_info)
        self.lbl_battery = QLabel("-")
        self.lbl_version = QLabel("-")
        self.lbl_temp = QLabel("-")
        self.lbl_rate = QLabel("-")
        form.addRow("电量", self.lbl_battery)
        form.addRow("版本", self.lbl_version)
        form.addRow("温度", self.lbl_temp)
        form.addRow("采样率", self.lbl_rate)
        left.addWidget(grp_info)

        grp_val = QGroupBox("实时数值")
        vgrid = QGridLayout(grp_val)
        self.value_labels: Dict[str, QLabel] = {}
        rows = [("加速度 (g)", ["ax", "ay", "az"]), ("角速度 (°/s)", ["gx", "gy", "gz"]),
                ("角度 (°)", ["roll", "pitch", "yaw"]), ("磁场 (mG)", ["mx", "my", "mz"]),
                ("四元数", ["q0", "q1", "q2", "q3"])]
        r = 0
        for title, keys in rows:
            vgrid.addWidget(QLabel(f"<b>{title}</b>"), r, 0, 1, 2)
            r += 1
            for k in keys:
                vgrid.addWidget(QLabel(k.upper()), r, 0)
                lab = QLabel("0.000")
                lab.setMinimumWidth(90)
                lab.setStyleSheet("font-family:monospace")
                vgrid.addWidget(lab, r, 1)
                self.value_labels[k] = lab
                r += 1
        left.addWidget(grp_val)

        self.attitude = AttitudeIndicator()
        left.addWidget(self.attitude)
        left.addStretch(1)

        # 中间：曲线
        mid = QVBoxLayout()
        body.addLayout(mid, 1)
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("显示通道："))
        self.ch_boxes: Dict[str, QCheckBox] = {}
        for name in CHANNELS:
            cb = QCheckBox(name)
            cb.setChecked(name in ("加速度 (g)", "角度 (°)"))
            self.ch_boxes[name] = cb
            ctrl.addWidget(cb)
        ctrl.addStretch(1)
        ctrl.addWidget(QLabel("窗口(秒)"))
        self.spin_window = QDoubleSpinBox()
        self.spin_window.setRange(1, 120)
        self.spin_window.setValue(10)
        ctrl.addWidget(self.spin_window)
        self.chk_autoscale = QCheckBox("自动缩放")
        self.chk_autoscale.setChecked(True)
        ctrl.addWidget(self.chk_autoscale)
        mid.addLayout(ctrl)

        self.plot = pg.PlotWidget(background="#101820")
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.addLegend(offset=(10, 10))
        self.plot.setLabel("bottom", "时间", units="s")
        self.plot.setMouseEnabled(x=True, y=True)
        mid.addWidget(self.plot, 1)

        # 右列：配置 / 校准 / 记录
        right = QTabWidget()
        right.setMaximumWidth(340)
        body.addWidget(right, 0)
        right.addTab(self._build_config_tab(), "配置")
        right.addTab(self._build_calib_tab(), "校准")
        right.addTab(self._build_record_tab(), "记录")

        # 底部日志
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(120)
        self.log.setStyleSheet("font-family:monospace")
        outer.addWidget(self.log)

    def _combo(self, options, default_index=0) -> QComboBox:
        cmb = QComboBox()
        for value, label in options:
            cmb.addItem(label, value)
        cmb.setCurrentIndex(default_index)
        return cmb

    def _build_config_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()

        self.cmb_rate = self._combo(RATE_OPTIONS, 5)
        self.btn_apply_rate = QPushButton("应用")
        self.btn_apply_rate.clicked.connect(
            lambda: self._call("set_rate", self.cmb_rate.currentData(), success="回传速率已下发"))
        form.addRow("回传速率", self._row(self.cmb_rate, self.btn_apply_rate))

        self.cmb_bw = self._combo(BW_OPTIONS, 4)
        self.btn_apply_bw = QPushButton("应用")
        self.btn_apply_bw.clicked.connect(
            lambda: self._call("set_bandwidth", self.cmb_bw.currentData(), success="带宽已下发"))
        form.addRow("带宽", self._row(self.cmb_bw, self.btn_apply_bw))

        self.cmb_algo = self._combo([(0, "9 轴（磁力解算，绝对航向）"), (1, "6 轴（积分航向，相对航向）")])
        self.btn_apply_algo = QPushButton("应用")
        self.btn_apply_algo.clicked.connect(
            lambda: self._call("set_algorithm", self.cmb_algo.currentData(), success="算法已下发"))
        form.addRow("算法", self._row(self.cmb_algo, self.btn_apply_algo))

        self.cmb_orient = self._combo([(0, "水平安装"), (1, "垂直安装（Y 轴箭头朝上）")])
        self.btn_apply_orient = QPushButton("应用")
        self.btn_apply_orient.clicked.connect(
            lambda: self._call("set_orientation", self.cmb_orient.currentData(), success="安装方向已下发"))
        form.addRow("安装方向", self._row(self.cmb_orient, self.btn_apply_orient))

        self.cmb_out = self._combo([(0, "加速度 + 角速度 + 角度"), (1, "位移 + 位移速度 + 角度")])
        self.btn_apply_out = QPushButton("应用")
        self.btn_apply_out.clicked.connect(self._apply_output_mode)
        form.addRow("数据包内容", self._row(self.cmb_out, self.btn_apply_out))

        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("例如 WT12345678")
        self.btn_set_name = QPushButton("下发")
        self.btn_set_name.clicked.connect(self._set_name)
        form.addRow("蓝牙名称", self._row(self.edit_name, self.btn_set_name))

        lay.addLayout(form)

        row = QHBoxLayout()
        btn_save = QPushButton("保存配置")
        btn_save.clicked.connect(lambda: self._call("save", success="已发送保存指令"))
        btn_default = QPushButton("恢复默认并保存")
        btn_default.clicked.connect(self._restore_default)
        btn_reboot = QPushButton("重启设备")
        btn_reboot.clicked.connect(self._reboot)
        for b in (btn_save, btn_default, btn_reboot):
            row.addWidget(b)
        lay.addLayout(row)
        lay.addWidget(QLabel("提示：所有写入指令都会按协议先解锁（FF AA 69 88 B5）再保存。"))
        lay.addStretch(1)
        return w

    def _build_calib_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        grp = QGroupBox("加速度校准")
        g = QVBoxLayout(grp)
        g.addWidget(QLabel("保持模块水平静止，然后点击："))
        btn = QPushButton("开始加速度校准")
        btn.clicked.connect(lambda: self._confirm_then(
            "加速度校准", "请确认模块已水平静止放置，是否继续？",
            lambda: self._call("acc_calibration", success="加速度校准指令已下发")))
        g.addWidget(btn)
        lay.addWidget(grp)

        grp2 = QGroupBox("磁场校准（向导）")
        g2 = QVBoxLayout(grp2)
        g2.addWidget(QLabel("1. 点击“进入磁场校准”\n2. 绕 X/Y/Z 三轴各缓慢转 3 圈\n3. 点击“完成并保存”"))
        btn_start = QPushButton("进入磁场校准")
        btn_start.clicked.connect(lambda: self._call("mag_calibration_start", success="已进入磁场校准"))
        btn_stop = QPushButton("完成并保存")
        btn_stop.clicked.connect(lambda: self._call("mag_calibration_finish", success="已退出校准并保存"))
        g2.addWidget(btn_start)
        g2.addWidget(btn_stop)
        lay.addWidget(grp2)

        grp3 = QGroupBox("角度")
        g3 = QVBoxLayout(grp3)
        btn_zero = QPushButton("Z 轴角度归零")
        btn_zero.clicked.connect(lambda: self._confirm_then(
            "Z 轴归零", "该指令需要模块当前为六轴算法才生效，是否继续？",
            lambda: self._call("yaw_zero", success="Z 轴角度归零指令已下发")))
        btn_ref = QPushButton("把当前姿态设为角度参考")
        btn_ref.clicked.connect(lambda: self._call("angle_reference", success="角度参考指令已下发"))
        g3.addWidget(btn_zero)
        g3.addWidget(btn_ref)
        lay.addWidget(grp3)
        lay.addStretch(1)
        return w

    def _build_record_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.btn_record = QPushButton("开始记录 CSV")
        self.btn_record.clicked.connect(self._toggle_record)
        lay.addWidget(self.btn_record)
        self.lbl_record = QLabel("未记录")
        lay.addWidget(self.lbl_record)
        lay.addWidget(QLabel("记录的通道：加速度、角速度、角度、位移/速度、磁场、四元数"))
        lay.addStretch(1)
        return w

    @staticmethod
    def _row(*widgets) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        for widget in widgets:
            lay.addWidget(widget)
        return w

    # ---------------- 信号连接
    def _connect_signals(self) -> None:
        self.btn_scan.clicked.connect(lambda: (self._log("扫描中…"), self.worker.do_scan()))
        self.btn_connect.clicked.connect(self._connect)
        self.btn_disconnect.clicked.connect(self.worker.do_disconnect)
        self.btn_info.clicked.connect(self.worker.do_read_info)
        self.spin_window.valueChanged.connect(lambda v: setattr(self, "window_seconds", float(v)))

        self.worker.statusMsg.connect(self._log)
        self.worker.errorMsg.connect(lambda m: self._log(f"错误：{m}"))
        self.worker.scanResult.connect(self._on_scan)
        self.worker.infoReady.connect(self._on_info)
        self.worker.auxReady.connect(self._on_aux)
        self.worker.connChanged.connect(self._on_conn)
        self.worker.sampleReady.connect(self._on_sample)

    # ---------------- 动作
    def _connect(self) -> None:
        addr = self.cmb_dev.currentData() if self.cmb_dev.count() else None
        self.worker.do_connect(addr)

    def _call(self, method: str, *args, success: Optional[str] = None) -> None:
        self.worker.do_call(method, *args, success=success)

    def _confirm_then(self, title: str, text: str, action) -> None:
        if QMessageBox.question(self, title, text) == QMessageBox.Yes:
            action()

    def _restore_default(self) -> None:
        self._confirm_then("恢复默认配置", "将恢复出厂默认配置并保存，是否继续？",
                           lambda: self._call("restore_default", success="已下发恢复默认指令"))

    def _reboot(self) -> None:
        self._confirm_then("重启设备", "设备重启后需要重新连接，是否继续？",
                           lambda: self._call("reboot", success="已下发重启指令"))

    def _apply_output_mode(self) -> None:
        mode = self.cmb_out.currentData()
        self.worker.mode = mode

        async def job():
            client = self.worker._ensure_client()
            client.mode = mode
            await client.set_output_mode(mode)

        self.worker.submit(job)
        self._log(f"数据包内容切换为 {'位移+位移速度+角度' if mode else '加速度+角速度+角度'}")

    def _set_name(self) -> None:
        name = self.edit_name.text().strip()
        if not name:
            return
        self._call("set_device_name", name, success="蓝牙名称指令已下发（需重启设备生效）")

    def _poll_aux(self) -> None:
        if self.last_sample is not None and self.worker.client is not None:
            self.worker.do_poll_aux(include_device=False)

    # ---------------- 回调
    def _on_scan(self, items) -> None:
        self.cmb_dev.clear()
        for address, name, rssi in items:
            self.cmb_dev.addItem(f"{address}   {name or '(无名)'}   RSSI {rssi}", address)
        self._log(f"扫描完成，发现 {len(items)} 个维特设备")
        if not items:
            QMessageBox.information(self, "扫描", "没有发现维特设备（名字以 WT/BWT 开头）。\n"
                                                  "请确认传感器已开机、未被手机/Windows 上位机占用。")

    def _on_conn(self, connected: bool) -> None:
        self.btn_connect.setEnabled(not connected)
        self.btn_disconnect.setEnabled(connected)
        self.btn_info.setEnabled(connected)
        self.lbl_state.setText("已连接" if connected else "未连接")
        self.lbl_state.setStyleSheet("color:#2ecc71" if connected else "color:#888888")
        if not connected:
            self.last_sample = None

    def _on_info(self, info: dict) -> None:
        bat = info.get("battery")
        if bat:
            self.lbl_battery.setText(f"{bat['percent']}%  ({bat['voltage']:.2f} V)")
        if "version" in info:
            self.lbl_version.setText(str(info["version"]))
        if "temperature" in info:
            self.lbl_temp.setText(f"{info['temperature']:.2f} ℃")
        if "magnetometer" in info:
            self.aux["magnetometer"] = info["magnetometer"]

    def _on_aux(self, aux: dict) -> None:
        self.aux.update(aux)
        if "battery" in aux:
            self.lbl_battery.setText(f"{aux['battery']['percent']}%  ({aux['battery']['voltage']:.2f} V)")
        if "temperature" in aux:
            self.lbl_temp.setText(f"{aux['temperature']:.2f} ℃")

    def _on_sample(self, sample: P.Sample) -> None:
        self.last_sample = sample
        self.samples_seen += 1
        t = sample.t - self.t0
        for key, value in (("ax", sample.ax), ("ay", sample.ay), ("az", sample.az),
                           ("gx", sample.gx), ("gy", sample.gy), ("gz", sample.gz),
                           ("roll", sample.roll), ("pitch", sample.pitch), ("yaw", sample.yaw),
                           ("pos_e", sample.pos_e), ("pos_n", sample.pos_n), ("pos_u", sample.pos_u),
                           ("vel_e", sample.vel_e), ("vel_n", sample.vel_n), ("vel_u", sample.vel_u)):
            self.buffers[key].append((t, value))
        if self.record_writer is not None:
            row = sample.as_dict()
            for k, key in (("mx", "magnetometer"), ("my", "magnetometer"), ("mz", "magnetometer")):
                mag = self.aux.get(key)
                row[k] = mag[["mx", "my", "mz"].index(k)] if mag else ""
            for i in range(4):
                quat = self.aux.get("quaternion")
                row[f"q{i}"] = quat[i] if quat else ""
            self.record_rows.append({k: row.get(k, "") for k in CSV_FIELDS})

    # ---------------- 刷新
    def _refresh(self) -> None:
        s = self.last_sample
        if s is not None:
            vals = {"ax": s.ax, "ay": s.ay, "az": s.az, "gx": s.gx, "gy": s.gy, "gz": s.gz,
                    "roll": s.roll, "pitch": s.pitch, "yaw": s.yaw}
            mag = self.aux.get("magnetometer")
            for i, key in enumerate(("mx", "my", "mz")):
                vals[key] = mag[i] if mag else 0.0
            quat = self.aux.get("quaternion")
            for i in range(4):
                vals[f"q{i}"] = quat[i] if quat else 0.0
            for key, lab in self.value_labels.items():
                lab.setText(f"{vals.get(key, 0.0):9.3f}")
            self.attitude.set_angles(s.roll, s.pitch)
            client = self.worker.client
            rate = client.sample_rate if client else 0.0
            self.lbl_rate.setText(f"{rate:5.1f} Hz")

        # 曲线
        active = [(name, ch) for name, ch in CHANNELS.items() if self.ch_boxes[name].isChecked()]
        wanted = {key for _n, ch in active for key, _l, _c in ch}
        # 只在必要时加减曲线
        for key in list(self.curves):
            if key not in wanted:
                self.plot.removeItem(self.curves.pop(key))
        now = time.time() - self.t0
        for name, ch in active:
            for key, label, color in ch:
                if key not in self.curves:
                    self.curves[key] = self.plot.plot(pen=pg.mkPen(color, width=1.5), name=label)
        if self.buffers["ax"]:
            for key, curve in self.curves.items():
                buf = self.buffers.get(key)
                if not buf:
                    continue
                xs, ys = [], []
                for t, v in reversed(buf):
                    if t < now - self.window_seconds:
                        break
                    xs.append(t)
                    ys.append(v)
                curve.setData(xs[::-1], ys[::-1])
            self.plot.setXRange(max(0.0, now - self.window_seconds), now, padding=0.02)
            if self.chk_autoscale.isChecked():
                self.plot.enableAutoRange(axis=pg.ViewBox.YAxis)
            else:
                self.plot.disableAutoRange(axis=pg.ViewBox.YAxis)

        if self.record_rows:
            self._flush_record()

    # ---------------- 记录
    def _toggle_record(self) -> None:
        if self.record_writer is None:
            path = os.path.join(os.getcwd(), time.strftime("wt9011_%Y%m%d_%H%M%S.csv"))
            path, _ = QFileDialog.getSaveFileName(self, "保存 CSV", path, "CSV 文件 (*.csv)")
            if not path:
                return
            self.record_fh = open(path, "w", newline="")
            self.record_writer = csv.DictWriter(self.record_fh, fieldnames=CSV_FIELDS)
            self.record_writer.writeheader()
            self.btn_record.setText("停止记录")
            self.lbl_record.setText(f"记录中：{path}")
            self._log(f"开始记录：{path}")
        else:
            self._flush_record()
            self.record_fh.close()
            self.record_fh = None
            self.record_writer = None
            self.btn_record.setText("开始记录 CSV")
            self.lbl_record.setText("未记录")
            self._log("记录结束")

    def _flush_record(self) -> None:
        if self.record_writer is None or not self.record_rows:
            return
        self.record_writer.writerows(self.record_rows)
        self.record_rows.clear()

    # ---------------- 其它
    def _log(self, msg: str) -> None:
        self.log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def closeEvent(self, event) -> None:
        self.aux_timer.stop()
        self.ui_timer.stop()
        if self.record_fh:
            self._flush_record()
            self.record_fh.close()
        self.worker.submit(lambda: self.worker.client.disconnect() if self.worker.client else asyncio.sleep(0))
        QTimer.singleShot(300, self._finish_close)
        event.ignore()

    def _finish_close(self) -> None:
        self.worker.shutdown()
        QApplication.instance().quit()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="WT9011DCL-BT50 Linux 上位机")
    parser.add_argument("--mock", action="store_true", help="模拟模式（无硬件演示）")
    parser.add_argument("--addr", help="设备 MAC 地址")
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1] + ["wt9011"])
    pg.setConfigOptions(antialias=True)
    win = MainWindow(mock=args.mock, address=args.addr)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
