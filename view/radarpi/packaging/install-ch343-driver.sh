#!/bin/bash
# 可选：为 CH343/CH344/CH9102 串口小板安装 WCH 厂商驱动（DKMS）
#
# 什么时候需要这个脚本？
#   * FT232 串口小板：不需要，Linux 内核自带 ftdi_sio，插上就是 /dev/ttyUSB0；
#   * CH343/CH344：先看裸插能不能用 —— 很多模块本身符合 CDC-ACM，
#     内核的 cdc_acm 会直接把它枚举成 /dev/ttyACM0，这时也不需要装驱动；
#   * 只有当 lsusb 能看到 1a86:55d3/55d4/55d8，但 /dev 下既没有 ttyACM 也没有
#     ttyUSB 时，才需要本脚本编译 WCH 的 out-of-tree 驱动。
#
# 需要联网 + 内核头文件。安装完设备名为 /dev/ttyCH343USB0。
#
# 用法： sudo ./packaging/install-ch343-driver.sh
#        sudo ./packaging/install-ch343-driver.sh --check   # 只检查，不安装

set -euo pipefail

DRIVER_REPO="https://github.com/WCHSoftGroup/ch343ser_linux.git"
WORKDIR="/usr/src/ch343ser_linux"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

info() { printf '%s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 sudo 运行"

info "==> 检查当前 USB 串口状态"
command -v lsusb >/dev/null 2>&1 && lsusb | grep -iE '1a86|0403|10c4' || info "  （没看到 1a86/0403/10c4 设备）"

if ls /dev/ttyACM* >/dev/null 2>&1 || ls /dev/ttyCH343USB* >/dev/null 2>&1; then
    info "  已存在 /dev/ttyACM* 或 /dev/ttyCH343USB*：当前驱动可用，无需安装。"
    info "  用 `radarpi ports` 看看哪个端口是雷达即可。"
    exit 0
fi

if ! lsusb 2>/dev/null | grep -q '1a86:'; then
    info "  没有检测到 WCH 芯片（1a86:）。如果你的串口小板是 FT232，不需要本脚本。"
    [ "$CHECK_ONLY" = "1" ] || info "  仍然继续安装 CH343 驱动……"
fi

[ "$CHECK_ONLY" = "1" ] && { info "仅检查模式，未做任何改动。"; exit 0; }

info "==> 安装编译依赖"
apt-get update -qq || true
apt-get install -y --no-install-recommends build-essential raspberrypi-kernel-headers git dkms \
    || apt-get install -y --no-install-recommends build-essential linux-headers-"$(uname -r)" git dkms

KERNEL="$(uname -r)"
KVER_MAJOR="$(echo "$KERNEL" | cut -d. -f1)"
KVER_MINOR="$(echo "$KERNEL" | cut -d. -f2)"

info "==> 获取 WCH 驱动源码"
if [ -d "$WORKDIR/.git" ]; then
    git -C "$WORKDIR" pull --ff-only || true
else
    rm -rf "$WORKDIR"
    git clone --depth 1 "$DRIVER_REPO" "$WORKDIR"
fi

# 内核 6.12 起 asm/unaligned.h 已改名为 linux/unaligned.h，WCH 源码尚未同步，
# 否则编译会报 "asm/unaligned.h: No such file or directory"
if [ "$KVER_MAJOR" -gt 6 ] || { [ "$KVER_MAJOR" -eq 6 ] && [ "$KVER_MINOR" -ge 12 ]; }; then
    info "==> 内核 $KERNEL 需要修正 unaligned.h 头文件引用"
    find "$WORKDIR" -name '*.c' -o -name '*.h' | while read -r f; do
        sed -i 's#<asm/unaligned.h>#<linux/unaligned.h>#g' "$f"
    done
fi

info "==> 编译并安装驱动"
cd "$WORKDIR"
if [ -d driver ]; then
    cd driver
fi

if command -v dkms >/dev/null 2>&1 && [ -f dkms.conf ]; then
    # 优先用 DKMS：内核升级后会自动重新编译
    DKMS_NAME="$(grep -E '^PACKAGE_NAME=' dkms.conf | cut -d'"' -f2 || echo ch343)"
    DKMS_VER="$(grep -E '^PACKAGE_VERSION=' dkms.conf | cut -d'"' -f2 || echo 1.0)"
    dkms remove -m "$DKMS_NAME" -v "$DKMS_VER" --all >/dev/null 2>&1 || true
    cp -r "$WORKDIR" "/usr/src/${DKMS_NAME}-${DKMS_VER}" 2>/dev/null || true
    dkms add -m "$DKMS_NAME" -v "$DKMS_VER" >/dev/null 2>&1 || true
    dkms build -m "$DKMS_NAME" -v "$DKMS_VER" || true
    dkms install -m "$DKMS_NAME" -v "$DKMS_VER" --force || {
        info "  DKMS 方式失败，改用 make 直接编译"
        make || die "编译失败，请把报错发给我们"
        make install || true
    }
else
    make || die "编译失败，请把报错发给我们"
    make install || install -m 0644 ch343.ko "/lib/modules/$KERNEL/kernel/drivers/usb/serial/"
fi

# 源码里若带了 cdc_acm 会抢设备，屏蔽掉避免冲突
echo "blacklist cdc_acm" > /etc/modprobe.d/radarpi-blacklist-cdc_acm.conf
echo "ch343" > /etc/modules-load.d/radarpi-ch343.conf
depmod -a

info "==> 加载驱动"
modprobe ch343 2>/dev/null || insmod "$(find /lib/modules/"$KERNEL" -name 'ch343.ko*' | head -1)" || true
udevadm control --reload-rules || true
udevadm trigger --subsystem-match=tty || true
sleep 1

if ls /dev/ttyCH343USB* >/dev/null 2>&1; then
    info "安装成功：$(ls /dev/ttyCH343USB* | tr '\n' ' ')"
    info "接下来执行： radarpi doctor --probe"
else
    info "驱动已安装，但还看不到 /dev/ttyCH343USB*。"
    info "请拔插一次串口小板，然后执行： dmesg | tail -20  查看内核日志。"
fi
