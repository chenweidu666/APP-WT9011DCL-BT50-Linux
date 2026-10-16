#!/usr/bin/env bash
# WT9011DCL-BT50 Linux 上位机启动脚本
#   ./run.sh            启动图形界面
#   ./run.sh --mock     用模拟数据启动（没有硬件时演示界面）
#   ./run.sh cli scan   走命令行
set -euo pipefail
cd "$(dirname "$0")"

PY=${PY:-python3}

if ! "$PY" -c "import bleak" 2>/dev/null; then
    echo "缺少依赖，先安装：pip install -r requirements.txt"
    exit 1
fi

if [[ "${1:-}" == "cli" ]]; then
    shift
    exec "$PY" -m wt9011.cli "$@"
fi

if ! "$PY" -c "import PySide6" 2>/dev/null; then
    echo "未安装 PySide6（图形界面依赖）。可执行：pip install PySide6 pyqtgraph"
    echo "或改用命令行：./run.sh cli scan"
    exit 1
fi

exec "$PY" -m wt9011.gui "$@"
