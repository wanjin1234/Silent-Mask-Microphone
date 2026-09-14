#!/usr/bin/env bash
# ============================================================
#  Silent-Mask-Microphone · 树莓派全新系统环境配置脚本
#  目标：Raspberry Pi OS (Bookworm 64-bit) 全新烧录后，一键装好
#        Python 3.11.4 + 全部运行依赖，使现行代码可直接运行。
#
#  用法（在树莓派上执行）：
#      cd ~/arDisplay
#      bash deploy/setup_pi.sh
#  或： chmod +x deploy/setup_pi.sh && ./deploy/setup_pi.sh
#
#  说明：
#    - 系统自带 python3 通常为 3.11.2，本脚本从源码编译 3.11.4
#      装到 /usr/local（make altinstall，不覆盖系统 python）。
#    - 编译较慢（树莓派约 15~40 分钟），默认关闭 PGO 优化；
#      设 ENABLE_OPT=1 可开启 --enable-optimizations（更慢，慎用）。
# ============================================================
set -euo pipefail

# ---------------- 可调参数 ----------------
PYVER="3.11.4"                        # 目标 Python 版本
PYURL="https://www.python.org/ftp/python/${PYVER}/Python-${PYVER}.tgz"
PROJECT_DIR="${HOME}/arDisplay"       # 项目根目录（与 scp 目标一致）
VENV="${PROJECT_DIR}/venv"
ENABLE_OPT="${ENABLE_OPT:-0}"         # 1=开启 PGO 优化编译
INSTALL_AI="${INSTALL_AI:-0}"         # 1=顺带装 AI 推理运行时（tflite）
JOBS="$(nproc)"

# 版本号拆成 3 / 11 / 4
IFS='.' read -r PY_MAJOR PY_MINOR PY_PATCH <<< "$PYVER"
PYBIN="python${PY_MAJOR}.${PY_MINOR}"   # 例如 python3.11

log() { echo ""; echo "==> $*"; }

log "[1/8] 更新系统并安装编译/运行依赖"
sudo apt-get update
sudo apt-get install -y \
  build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev \
  libsqlite3-dev libffi-dev liblzma-dev \
  wget git ca-certificates \
  i2c-tools \
  libsdl2-dev libsdl2-image-dev libsdl2-mixer-dev libsdl2-ttf-dev \
  libfreetype6-dev libjpeg-dev libpng-dev libportmidi-dev

log "[2/8] 检查/编译安装 Python ${PYVER}"
if command -v "$PYBIN" >/dev/null 2>&1 && \
   "$PYBIN" -c "import sys; exit(0 if sys.version_info[:3] == (${PY_MAJOR}, ${PY_MINOR}, ${PY_PATCH}) else 1)"; then
  echo "    已存在 Python ${PYVER}，跳过编译。"
else
  cd /tmp
  if [ ! -f "Python-${PYVER}.tgz" ]; then
    wget -O "Python-${PYVER}.tgz" "$PYURL"
  fi
  tar -xzf "Python-${PYVER}.tgz"
  cd "Python-${PYVER}"
  CONFIGURE_ARGS="--enable-shared --with-system-ffi"
  if [ "$ENABLE_OPT" = "1" ]; then CONFIGURE_ARGS="$CONFIGURE_ARGS --enable-optimizations"; fi
  # rpath 让编译出的 python 能直接找到 libpython3.11.so
  ./configure $CONFIGURE_ARGS LDFLAGS="-Wl,-rpath,/usr/local/lib"
  make -j"$JOBS"
  sudo make altinstall
  sudo ldconfig
  cd /tmp
fi

log "[3/8] 初始化 pip"
sudo "$PYBIN" -m ensurepip --upgrade || true
sudo "$PYBIN" -m pip install --upgrade pip setuptools wheel || true

log "[4/8] 建项目目录与 venv"
mkdir -p "$PROJECT_DIR"
if [ ! -x "${VENV}/bin/python" ]; then
  "$PYBIN" -m venv "$VENV"
fi

log "[5/8] 安装 Python 运行依赖（装进 venv）"
"${VENV}/bin/pip" install --upgrade pip
"${VENV}/bin/pip" install pyserial pigpio smbus2 numpy
# pygame 必须从源码编译：PyPI wheel 内置的 SDL2 无 kmsdrm 支持，
# 源码编译会通过 sdl2-config 链接系统 SDL2（树莓派官方带 kmsdrm），HDMI 才能显示。
"${VENV}/bin/pip" install --no-binary pygame pygame
if [ "$INSTALL_AI" = "1" ]; then
  "${VENV}/bin/pip" install ai-edge-litert || "${VENV}/bin/pip" install tflite-runtime || true
fi

log "[6/8] 开启 I2C（UPS 电量监测用）"
sudo raspi-config nonint do_i2c 0 || true
sudo modprobe i2c-dev || true

log "[7/8] 从源码编译 pigpio 并设置开机自启（新版系统已无 apt 包）"
if ! command -v pigpiod >/dev/null 2>&1; then
  cd /tmp
  git clone https://github.com/joan2937/pigpio.git
  cd pigpio
  make -j"$JOBS"
  sudo make install
  sudo ldconfig
  cd /tmp
else
  echo "    pigpiod 已存在，跳过编译。"
fi

sudo tee /etc/systemd/system/pigpiod.service >/dev/null <<'EOF'
[Unit]
Description=pigpio daemon
After=multi-user.target

[Service]
Type=forking
ExecStart=/usr/local/bin/pigpiod
ExecStop=/bin/kill -TERM $MAINPID

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now pigpiod

log "[8/8] 完成"
echo "Python : ${PYBIN} (${PYVER})"
echo "venv   : ${VENV}"
echo "依赖   : pygame pyserial pigpio smbus2 numpy"
echo ""
echo "下一步（回到 Windows 开发机执行 deploy\\sync_to_pi.ps1 同步代码），然后在树莓派："
echo "  cd ${PROJECT_DIR}"
echo "  bash run.sh"
echo ""
echo "若 /dev/i2c-1 未出现，重启一次： sudo reboot"
