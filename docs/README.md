# docs

本目录只放**我们自己的**内容；厂商文档不随仓库分发（避免转载他人文档）。

| 文件 | 说明 |
|---|---|
| `screenshot-gui.png` | 界面截图（模拟数据，由 `tools/smoke_gui.py --screenshot` 生成） |
| `语雀资料索引.md` | 官方资料链接索引（仅链接，不含厂商标文档正文） |

## 协议依据（厂商公开文档，请直接访问原文）

| 内容 | 链接 |
|---|---|
| 蓝牙 5.0 通讯协议（数据包格式、寄存器表、全部指令） | <https://wit-motion.yuque.com/wumwnr/docs/ycui87fgg1mepk1u> |
| 手机 APP 数据解析与指令发送 | <https://wit-motion.yuque.com/wumwnr/ltst03/clnc7v> |
| 蓝牙 5.0 快速上手 / 电脑蓝牙连接 | <https://wit-motion.yuque.com/wumwnr/ltst03/rorzux5fl78bswi3> |
| 官方示例仓库（Android / C# / Unity / iOS / ESP32 / **Python**） | <https://github.com/WITMOTION/WitBluetooth_BWT901BLE5_0> |
| BLE 5.0 协议说明（GitBook） | <https://support-73.gitbook.io/witmotion-sdk/ble-5.0-protocol> |

`wt9011/protocol.py` 里的常量、寄存器表和换算系数即依据上表实现，
每一项都能在协议文档里找到出处（单测里也用文档给出的字节样例做断言）。
