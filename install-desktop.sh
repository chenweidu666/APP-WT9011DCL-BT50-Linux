#!/usr/bin/env bash
# 把 WT9011DCL-BT50 上位机注册成 Ubuntu 应用（应用菜单 + 图标 + 命令行启动器）。
#
#   ./install-desktop.sh            安装/更新
#   ./install-desktop.sh --uninstall 卸载
#
# 做的事（都在用户目录，不需要 sudo）：
#   ~/.local/bin/wt9011              → 命令行入口
#   ~/.local/bin/wt9011-gui          → 图形界面入口
#   ~/.local/share/applications/wt9011-bt50.desktop        → 应用菜单：WT9011 上位机
#   ~/.local/share/icons/hicolor/{scalable,256x256}/apps/wt9011-bt50.* → 图标
# 注：`--mock` 模拟数据只用于离线开发/自测（tools/smoke_gui.py），不注册成菜单项。
set -euo pipefail

APP_ID="wt9011-bt50"
REPO="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_SCALABLE="$HOME/.local/share/icons/hicolor/scalable/apps"
ICON_PNG="$HOME/.local/share/icons/hicolor/256x256/apps"

die() { echo "错误：$*" >&2; exit 1; }

# ---------- 卸载 ----------
if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$BIN_DIR/wt9011" "$BIN_DIR/wt9011-gui"
    rm -f "$APP_DIR/$APP_ID.desktop" "$APP_DIR/$APP_ID-mock.desktop"
    rm -f "$ICON_SCALABLE/$APP_ID.svg" "$ICON_PNG/$APP_ID.png"
    update-desktop-database "$APP_DIR" 2>/dev/null || true
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
    echo "已卸载：$APP_ID"
    exit 0
fi

# ---------- 找一个装了依赖的 Python ----------
CANDIDATES=("${PY:-}" "$REPO/.venv/bin/python" "$(command -v python3 || true)" \
            "$HOME/miniconda3/bin/python3" "$HOME/anaconda3/bin/python3" "/usr/bin/python3")
PY=""
for cand in "${CANDIDATES[@]}"; do
    [[ -n "$cand" && -x "$cand" ]] || continue
    if "$cand" -c "import bleak, PySide6, pyqtgraph" >/dev/null 2>&1; then PY="$cand"; break; fi
done
[[ -n "$PY" ]] || die "找不到同时装了 bleak + PySide6 + pyqtgraph 的 Python。
请先执行：pip install -r requirements.txt
（或者：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt，再重跑本脚本）"

echo "使用解释器：$PY  ($("$PY" -c 'import sys;print(sys.version.split()[0])'))"

# ---------- 启动器 ----------
mkdir -p "$BIN_DIR" "$APP_DIR"
for entry in "wt9011:cli" "wt9011-gui:gui"; do
    name="${entry%%:*}"; mod="${entry##*:}"
    cat > "$BIN_DIR/$name" <<EOF
#!/usr/bin/env bash
# 由 WT9011DCL-BT50-Linux/install-desktop.sh 生成
export PYTHONPATH="$REPO\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PY" -m wt9011.$mod "\$@"
EOF
    chmod +x "$BIN_DIR/$name"
    echo "启动器：$BIN_DIR/$name"
done

# ---------- 图标 ----------
mkdir -p "$ICON_SCALABLE" "$ICON_PNG"
cp "$REPO/packaging/$APP_ID.svg" "$ICON_SCALABLE/$APP_ID.svg"
if command -v rsvg-convert >/dev/null 2>&1; then
    rsvg-convert -w 256 -h 256 "$REPO/packaging/$APP_ID.svg" -o "$ICON_PNG/$APP_ID.png"
    echo "图标：$ICON_SCALABLE/$APP_ID.svg + $ICON_PNG/$APP_ID.png"
else
    echo "图标：$ICON_SCALABLE/$APP_ID.svg（未装 rsvg-convert，跳过 PNG）"
fi

# ---------- 应用菜单条目 ----------
# 只有正式入口；--mock 属于离线开发用法，不注册到菜单里
cat > "$APP_DIR/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=WT9011 上位机
Name[en]=WT9011 Attitude Monitor
Comment=WitMotion WT9011DCL-BT50 attitude sensor over BLE 5.0 (Linux)
Comment[zh_CN]=维特智能 WT9011DCL-BT50 蓝牙 5.0 姿态传感器上位机
Exec=$BIN_DIR/wt9011-gui
TryExec=$BIN_DIR/wt9011-gui
Icon=$APP_ID
Terminal=false
StartupNotify=true
StartupWMClass=wt9011
Categories=Utility;
Keywords=WT9011;WT9011DCL-BT50;BWT901BLE5.0;WitMotion;维特;姿态;IMU;加速度;陀螺仪;蓝牙;BLE;
EOF
chmod 644 "$APP_DIR/$APP_ID.desktop"
echo "菜单条目：$APP_DIR/$APP_ID.desktop"

# 清掉历史版本的模拟演示条目
rm -f "$APP_DIR/$APP_ID-mock.desktop"

# ---------- 刷新缓存 ----------
update-desktop-database "$APP_DIR" 2>/dev/null || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
command -v desktop-file-validate >/dev/null 2>&1 && \
    desktop-file-validate "$APP_DIR/$APP_ID.desktop" && echo "desktop 文件校验通过"

# ---------- PATH 提示 ----------
case ":$PATH:" in
    *":$BIN_DIR:"*) : ;;
    *) echo "提示：$BIN_DIR 不在 PATH 里，命令行用 wt9011-gui 前需要加入 PATH（GUI 菜单不受影响）" ;;
esac

echo
echo "完成。应用菜单里搜索 “WT9011” 即可启动；命令行可用：wt9011 scan / wt9011-gui"
