"""GUI 冒烟测试：offscreen 起界面 + 模拟数据，跑一段时间后走真实关闭流程。

用途：没有硬件也能验证界面/绘图/记录/关闭链路是否正常（CI 或改完代码自检）。
    python3 tools/smoke_gui.py                        # 默认 5 秒，不存图
    python3 tools/smoke_gui.py --seconds 6 --screenshot docs/screenshot-gui.png
退出码：0 = 正常，1 = 有异常，3 = 关闭卡住（看门狗）。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import pyqtgraph as pg  # noqa: E402
from wt9011.gui import CSV_FIELDS, MainWindow  # noqa: E402

T0 = time.time()
errors: list[str] = []


def hook(exc_type, exc, tb):  # noqa: ANN001
    import traceback

    errors.append("".join(traceback.format_exception(exc_type, exc, tb)))


def log(msg: str) -> None:
    print(f"[{time.time() - T0:5.1f}s] {msg}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="WT9011DCL-BT50 GUI 冒烟测试")
    parser.add_argument("--seconds", type=float, default=5.0, help="界面运行时长")
    parser.add_argument("--screenshot", help="截图保存路径")
    args = parser.parse_args()

    sys.excepthook = hook
    app = QApplication(sys.argv[:1] + ["smoke"])
    pg.setConfigOptions(antialias=True)
    win = MainWindow(mock=True)
    win.show()
    win.worker.do_connect("MOCK:00:11:22:33:44:55")
    log("窗口已显示（模拟模式），开始连接")

    rec_path = os.path.join(tempfile.mkdtemp(), "rec.csv")

    def step_record():
        log(f"连接状态={win.lbl_state.text()} 采样率={win.lbl_rate.text()} 曲线={len(win.curves)} "
            f"yaw={win.value_labels['yaw'].text().strip()}")
        win.record_fh = open(rec_path, "w", newline="")
        win.record_writer = csv.DictWriter(win.record_fh, fieldnames=CSV_FIELDS)
        win.record_writer.writeheader()
        log("开始记录 CSV")
        QTimer.singleShot(1500, step_close)

    def step_close():
        win._flush_record()
        win.record_fh.close()
        with open(rec_path) as fh:
            rows = fh.read().strip().splitlines()
        log(f"CSV 记录 {len(rows) - 1} 行")
        if args.screenshot:
            os.makedirs(os.path.dirname(os.path.abspath(args.screenshot)), exist_ok=True)
            win.grab().save(args.screenshot)
            log(f"截图已保存：{args.screenshot}")
        log("调用 win.close()（真实关闭流程）")
        win.close()

    QTimer.singleShot(int(args.seconds * 1000), step_record)
    QTimer.singleShot(int((args.seconds + 18) * 1000), lambda: (log("!! 看门狗：关闭流程卡住"), os._exit(3)))

    rc = app.exec()
    log(f"事件循环退出 rc={rc} 异常数={len(errors)}")
    for err in errors:
        print(err, flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
