#!/usr/bin/env python3
"""
树莓派蓝牙立体声音箱一键配置（旧版内核 6.18.34 / 6.6 专用）
"""

import os, sys, subprocess, time, shutil

def run(cmd, check=True, shell=True, echo=True):
    if echo: print(f"\033[36m[执行] {cmd}\033[0m")
    result = subprocess.run(cmd, shell=shell, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"\033[31m[错误] 命令返回 {result.returncode}\n{result.stderr}\033[0m")
        sys.exit(1)
    return result.stdout.strip()

def write_file(path, content):
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    with open(path, "w") as f:
        f.write(content)

def check_root():
    if os.geteuid() != 0:
        print("\033[31m请使用 sudo 运行！\033[0m")
        sys.exit(1)

def install_seeed_driver():
    print("\n\033[1;33m[驱动] 安装 seeed-voicecard\033[0m")
    repo_dir = "/tmp/seeed-voicecard"
    if os.path.exists(repo_dir):
        run(f"rm -rf {repo_dir}")
    urls = [
        "https://github.com/HinTak/seeed-voicecard.git",
        "https://hub.fastgit.xyz/HinTak/seeed-voicecard.git",
        "https://ghproxy.com/https://github.com/HinTak/seeed-voicecard.git",
    ]
    cloned = False
    for url in urls:
        print(f"尝试 git clone {url}")
        ret = os.system(f"git clone --depth=1 {url} {repo_dir} 2>/dev/null")
        if ret == 0 and os.path.exists(f"{repo_dir}/install.sh"):
            cloned = True
            break
    if not cloned:
        print("\033[31m无法克隆 seeed-voicecard，请手动下载并运行 install.sh\033[0m")
        sys.exit(1)
    os.chdir(repo_dir)
    run("apt install -y raspberrypi-kernel-headers dkms git i2c-tools libasound2-plugins", check=False)
    run("./install.sh", check=False)
    os.chdir("/home/pi")

def main():
    check_root()
    print("\033[1;34m=== 蓝牙立体声音箱配置 ===\033[0m")

    # 1. 基础软件
    for _ in range(6):
        if os.path.exists("/var/lib/apt/lists/lock"):
            os.system("killall -9 packagekitd 2>/dev/null; rm -f /var/lib/apt/lists/lock /var/cache/apt/archives/lock /var/lib/dpkg/lock-frontend")
            time.sleep(5)
    run("apt update")
    run("apt install -y bluez bluez-tools bluez-alsa-utils alsa-utils wget git")

    # 2. 设备树
    config = "/boot/firmware/config.txt"
    run(f"cp {config} {config}.bak")
    run(f"sed -i 's/^dtparam=audio=on/#dtparam=audio=on/' {config}")
    run(f"sed -i 's/^#dtparam=i2c_arm=on/dtparam=i2c_arm=on/' {config}")
    run(f"sed -i 's/^dtparam=i2c_arm=off/#dtparam=i2c_arm=off/' {config}")
    with open(config, "r") as f:
        if "seeed-2mic-voicecard" not in f.read():
            with open(config, "a") as fw: fw.write("\ndtoverlay=seeed-2mic-voicecard\n")

    # 3. 声卡驱动
    install_seeed_driver()

    # 4. 禁用 PipeWire
    for srv in ["pipewire", "pipewire-pulse", "wireplumber"]:
        for scope in ["--user", "--global"]:
            run(f"systemctl {scope} stop {srv}.socket {srv}.service 2>/dev/null", check=False)
            run(f"systemctl {scope} disable {srv}.socket {srv}.service 2>/dev/null", check=False)
            run(f"systemctl {scope} mask {srv}.service 2>/dev/null", check=False)
        run(f"systemctl mask {srv}.service 2>/dev/null", check=False)

    # 5. 蓝牙 main.conf
    mc = "/etc/bluetooth/main.conf"
    run(f"cp {mc} {mc}.bak")
    run(f"sed -i 's/^#Class =.*/Class = 0x200404/' {mc}")
    run(f"sed -i 's/^Class =.*/Class = 0x200404/' {mc}")
    run(f"sed -i 's/^#DiscoverableTimeout =.*/DiscoverableTimeout = 0/' {mc}")
    run(f"sed -i 's/^DiscoverableTimeout =.*/DiscoverableTimeout = 0/' {mc}")
    with open(mc, "r") as f:
        c = f.read()
    if "Class = 0x200404" not in c: run(f"echo '\nClass = 0x200404' >> {mc}")
    if "DiscoverableTimeout = 0" not in c: run(f"echo '\nDiscoverableTimeout = 0' >> {mc}")
    run("systemctl restart bluetooth"); time.sleep(2)
    run("rfkill unblock bluetooth")
    run("hciconfig hci0 up")
    run("hciconfig hci0 class 0x200404")
    run("bluetoothctl discoverable on")

    # 6. bt-agent
    write_file("/etc/systemd/system/bt-agent.service", """[Unit]
Description=Bluetooth Agent (Auto Accept)
Requires=bluetooth.service
After=bluetooth.service
[Service]
Type=simple
ExecStart=/usr/bin/bt-agent -c NoInputNoOutput -p /etc/bluetooth/pin.conf
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
""")
    run("touch /etc/bluetooth/pin.conf")
    run("systemctl daemon-reload")
    run("systemctl enable bt-agent.service")
    run("systemctl start bt-agent.service")

    # 7. bt-speaker
    write_file("/etc/systemd/system/bt-speaker.service", """[Unit]
Description=Bluetooth Speaker (Stable)
After=bluetooth.service bt-agent.service
Requires=bluetooth.service bt-agent.service
[Service]
Type=simple
ExecStartPre=/bin/sleep 3
ExecStart=/usr/bin/bluealsa-aplay -S -D plughw:seeed2micvoicec 00:00:00:00:00:00
Restart=always
RestartSec=1
OOMScoreAdjust=-1000
[Install]
WantedBy=multi-user.target
""")
    run("systemctl daemon-reload")
    run("systemctl enable bt-speaker.service")

    # 8. 音量
    run("amixer -c seeed2micvoicec sset PCM 100 unmute", check=False)
    run("amixer -c seeed2micvoicec sset HP 100 unmute", check=False)
    run("amixer -c seeed2micvoicec sset 'HP DAC' 100 unmute", check=False)
    run("alsactl store", check=False)
    write_file("/etc/systemd/system/volume-max.service", """[Unit]
Description=Restore max volume at boot
After=bt-speaker.service
[Service]
Type=oneshot
ExecStart=/bin/bash -c "sleep 2 && alsactl restore -f /var/lib/alsa/asound.state"
[Install]
WantedBy=multi-user.target
""")
    run("systemctl daemon-reload")
    run("systemctl enable volume-max.service")

    # 9. 开机可见
    write_file("/etc/systemd/system/bt-discoverable.service", """[Unit]
Description=Set Bluetooth Discoverable at boot
After=bluetooth.service
Requires=bluetooth.service
[Service]
Type=oneshot
ExecStart=/usr/bin/bluetoothctl discoverable on
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
""")
    run("systemctl daemon-reload")
    run("systemctl enable bt-discoverable.service")
    run("systemctl start bt-discoverable.service")

    # 10. 启动
    run("systemctl start bt-speaker.service")
    time.sleep(2)
    print("\033[1;34m=== 配置完成，5 秒后重启 ===\033[0m")
    time.sleep(5)
    run("reboot")

if __name__ == "__main__":
    main()