#!/bin/bash
# radarpi 源码安装脚本（不依赖 dpkg，适合直接把源码目录拷到树莓派上安装）
#
# 用法（在源码目录里执行）：
#     sudo ./packaging/install.sh              # 完整安装并启动服务
#     sudo ./packaging/install.sh --no-service # 只装程序，不装/不启动 systemd 服务
#     sudo ./packaging/install.sh --uninstall  # 卸载（保留录制数据）
#
# 与 .deb 包的安装结果完全一致：同一个 postinst 负责建用户、修权限、装 udev 规则。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SRC="$ROOT/src"
LIBDIR="/usr/lib/radarpi"
DOCDIR="/usr/share/doc/radarpi"
CONFDIR="/etc/radarpi"
DATADIR="/var/lib/radarpi"

NO_SERVICE=0
UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --no-service) NO_SERVICE=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help)
            sed -n '2,10p' "$0"
            exit 0
            ;;
        *) echo "未知参数：$arg"; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 sudo 运行： sudo $0 $*"
    exit 1
fi

# --------------------------------------------------------------------------
if [ "$UNINSTALL" = "1" ]; then
    echo "==> 卸载 radarpi"
    if [ -f "$HERE/postrm" ]; then
        sh "$HERE/postrm" remove || true
        sh "$HERE/postrm" purge || true
    fi
    rm -rf "$LIBDIR" "$DOCDIR"
    rm -f /usr/bin/radarpi
    rm -f /lib/systemd/system/radarpi-web.service
    rm -f /lib/udev/rules.d/99-radarpi.rules
    command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload || true
    command -v udevadm >/dev/null 2>&1 && udevadm control --reload-rules || true
    echo "已卸载。录制数据（$DATADIR）与配置（$CONFDIR）已一并删除。"
    exit 0
fi

# --------------------------------------------------------------------------
echo "==> 检查运行环境"
KERNEL="$(uname -r)"
MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo 未知设备)"
PYVER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo missing)"
echo "    设备: $MODEL"
echo "    内核: $KERNEL"
echo "    python3: $PYVER"

if [ "$PYVER" = "missing" ]; then
    echo "错误：没有找到 python3。请先执行： sudo apt update && sudo apt install -y python3"
    exit 1
fi

# 内置的标准库串口实现就够了；pyserial 只是锦上添花
if python3 -c "import serial" >/dev/null 2>&1; then
    echo "    串口后端: pyserial 已安装"
else
    echo "    串口后端: 使用内置标准库实现（无需额外安装）"
fi

# pygame 桌面版与 ndarray 矩阵接口需要 numpy / pygame（网页版不需要）
if python3 -c "import numpy, pygame" >/dev/null 2>&1; then
    echo "    pygame 界面: 依赖齐全（python3-numpy + python3-pygame）"
else
    echo "    pygame 界面: 缺少依赖，如需桌面版（radarpi view）请执行："
    echo "                 sudo apt install -y python3-numpy python3-pygame fonts-noto-cjk"
fi

echo "==> 复制程序文件"
install -d -m 0755 "$LIBDIR" "$DOCDIR" "$CONFDIR"
rm -rf "$LIBDIR/radarpi"
cp -r "$SRC/radarpi" "$LIBDIR/radarpi"
find "$LIBDIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

cat > /usr/bin/radarpi <<'EOF'
#!/bin/sh
# radarpi 启动器
export PYTHONPATH="/usr/lib/radarpi${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONIOENCODING="utf-8"
exec python3 -m radarpi "$@"
EOF
chmod 0755 /usr/bin/radarpi

if [ -f "$CONFDIR/radarpi.conf" ]; then
    echo "    保留已有配置 $CONFDIR/radarpi.conf"
else
    install -m 0644 "$HERE/radarpi.conf" "$CONFDIR/radarpi.conf"
fi

install -m 0644 "$HERE/99-radarpi.rules" /lib/udev/rules.d/99-radarpi.rules
install -d -m 0755 /usr/share/applications
install -m 0644 "$HERE/radarpi-viewer.desktop" /usr/share/applications/radarpi-viewer.desktop
install -m 0644 "$ROOT/README.md" "$DOCDIR/README.md" 2>/dev/null || true
for doc in "使用说明.md" "使用说明.pdf"; do
    [ -f "$ROOT/docs/$doc" ] && install -m 0644 "$ROOT/docs/$doc" "$DOCDIR/$doc"
done
for shot in "pygame_console.png" "pygame_console_processor.png"; do
    [ -f "$ROOT/docs/$shot" ] && install -m 0644 "$ROOT/docs/$shot" "$DOCDIR/$shot"
done
if [ -f "$ROOT/examples/processor_demo.py" ]; then
    install -d -m 0755 "$DOCDIR/examples"
    install -m 0644 "$ROOT/examples/processor_demo.py" "$DOCDIR/examples/processor_demo.py"
fi

if [ "$NO_SERVICE" = "0" ]; then
    install -m 0644 "$HERE/radarpi-web.service" /lib/systemd/system/radarpi-web.service
else
    rm -f /lib/systemd/system/radarpi-web.service
fi

echo "==> 配置用户、权限与 udev 规则"
# 复用 .deb 的 postinst，保证两条安装路径行为一致
if [ -f "$HERE/postinst" ]; then
    if [ "$NO_SERVICE" = "0" ]; then
        sh "$HERE/postinst" configure
    else
        RADARPI_NO_SERVICE=1 sh "$HERE/postinst" configure
    fi
else
    echo "警告：找不到 postinst，跳过用户与权限配置"
fi

echo "==> 验证安装"
/usr/bin/radarpi version
echo "如需体检请执行： radarpi doctor"
