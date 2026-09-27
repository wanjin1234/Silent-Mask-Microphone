#!/usr/bin/env python3
"""生成 Raspberry Pi OS 可直接安装的 .deb 包。

不需要 docker / dpkg-deb / Linux，只要有 Python 3 就能在这里把包装好::

    python packaging/build_deb.py            # 输出到 dist/
    python packaging/build_deb.py --verify    # 打包后再自检一遍

生成的包用 ``sudo apt install ./dist/radarpi_1.0.0_all.deb`` 安装，
依赖只有 python3（Raspberry Pi OS 自带），装完即可离线运行。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import struct
import sys
import tarfile
import time
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
STATIC = os.path.join(SRC, "radarpi", "static")

PACKAGE = "radarpi"
ARCH = "all"

#: 让 python3 能 import 到 /usr/lib/radarpi 下的包
LAUNCHER = """#!/bin/sh
# radarpi 启动器：把打包目录加入 sys.path 后调用 Python 模块
export PYTHONPATH="/usr/lib/radarpi${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONIOENCODING="utf-8"
exec python3 -m radarpi "$@"
"""

COPYRIGHT = """Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: radarpi
Source: 随雷达设备一同交付的树莓派上位机源码

Files: *
Copyright: 2026 radarpi contributors
License: MIT
 Permission is hereby granted, free of charge, to any person obtaining a copy
 of this software and associated documentation files (the "Software"), to deal
 in the Software without restriction, including without limitation the rights
 to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 copies of the Software, and to permit persons to whom the Software is
 furnished to do so, subject to the following conditions:
 .
 The above copyright notice and this permission notice shall be included in
 all copies or substantial portions of the Software.
 .
 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
"""


# --------------------------------------------------------------------------
# 文件清单
# --------------------------------------------------------------------------


class Entry:
    """一个待打包文件/目录。"""

    def __init__(self, install_path: str, source: Optional[str] = None, data: Optional[bytes] = None,
                 mode: int = 0o644, is_dir: bool = False, conffile: bool = False):
        self.install_path = install_path.lstrip("/")
        self.source = source
        self.data = data
        self.mode = mode
        self.is_dir = is_dir
        self.conffile = conffile

    def read(self) -> bytes:
        if self.data is not None:
            return self.data
        with open(self.source, "rb") as fh:
            return fh.read()


def read_version() -> str:
    init = os.path.join(SRC, "radarpi", "__init__.py")
    with open(init, "r", encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r'^__version__\s*=\s*["\']([^"\']+)["\']', line)
            if m:
                return m.group(1)
    return "0.0.0"


def collect_entries() -> List[Entry]:
    entries: List[Entry] = []

    entries.append(Entry("usr/bin/radarpi", data=LAUNCHER.encode("utf-8"), mode=0o755))

    pkg_dir = os.path.join(SRC, "radarpi")
    for dirpath, dirnames, filenames in os.walk(pkg_dir):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, SRC).replace(os.sep, "/")
            entries.append(Entry("usr/lib/radarpi/" + rel, source=full))

    entries.append(
        Entry("etc/radarpi/radarpi.conf", source=os.path.join(HERE, "radarpi.conf"), conffile=True)
    )
    entries.append(Entry("lib/systemd/system/radarpi-web.service", source=os.path.join(HERE, "radarpi-web.service")))
    entries.append(Entry("lib/udev/rules.d/99-radarpi.rules", source=os.path.join(HERE, "99-radarpi.rules")))
    # 桌面菜单里的 pygame 上位机入口（只有装了桌面环境的系统才会显示）
    entries.append(
        Entry("usr/share/applications/radarpi-viewer.desktop",
              source=os.path.join(HERE, "radarpi-viewer.desktop"))
    )

    entries.append(Entry("usr/share/doc/radarpi/copyright", data=COPYRIGHT.encode("utf-8")))
    readme = os.path.join(ROOT, "README.md")
    if os.path.exists(readme):
        entries.append(Entry("usr/share/doc/radarpi/README.md", source=readme))
    for doc_name in ("使用说明.md", "使用说明.pdf"):
        doc = os.path.join(ROOT, "docs", doc_name)
        if os.path.exists(doc):
            entries.append(Entry("usr/share/doc/radarpi/" + doc_name, source=doc))
    # 界面实拍图（帮助使用者确认"跑起来应该长什么样"）
    for shot in ("pygame_console.png", "pygame_console_processor.png"):
        img = os.path.join(ROOT, "docs", shot)
        if os.path.exists(img):
            entries.append(Entry("usr/share/doc/radarpi/" + shot, source=img))
    # 示例算法处理器（ndarray 接口的样板）
    example = os.path.join(ROOT, "examples", "processor_demo.py")
    if os.path.exists(example):
        entries.append(Entry("usr/share/doc/radarpi/examples/processor_demo.py", source=example))

    # 录制目录（postinst 里会把属主改成 radarpi:dialout）
    entries.append(Entry("var/lib/radarpi", mode=0o755, is_dir=True))
    return entries


# --------------------------------------------------------------------------
# 包元数据
# --------------------------------------------------------------------------


def build_control(version: str, installed_size_kb: int) -> bytes:
    lines = [
        "Package: %s" % PACKAGE,
        "Version: %s" % version,
        "Section: misc",
        "Priority: optional",
        "Architecture: %s" % ARCH,
        "Depends: python3 (>= 3.7)",
        "Recommends: python3-serial, python3-numpy, python3-pygame",
        "Suggests: raspberrypi-kernel-headers, dkms, fonts-noto-cjk",
        "Installed-Size: %d" % installed_size_kb,
        "Maintainer: radarpi maintainers <radarpi@example.com>",
        "Description: 4D imaging mmWave radar console for Raspberry Pi OS",
        " radarpi is a pure-Python replacement for the vendor's Windows-only",
        " radar configuration tool. It talks to the 4D imaging radar over a",
        " 3000000 8N1 USB serial link, decodes the point-cloud protocol and",
        " serves a browser based console (3D point cloud, projections, point",
        " table, recording).",
        " .",
        " Features:",
        "  * auto-detect the radar's serial port, auto reconnect",
        "  * send the documented radar configuration commands",
        "  * live point-cloud web console on port 8080",
        "  * pygame desktop console for HDMI / VNC setups (`radarpi view`)",
        "  * point cloud to numpy ndarray interface (BEV / voxel matrices)",
        "  * CSV / JSONL recording and playback",
        "  * `radarpi doctor` environment diagnostics",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


# --------------------------------------------------------------------------
# tar / ar 组装
# --------------------------------------------------------------------------


def _tar_add_file(tar: tarfile.TarInfo, data: bytes, tar_obj: tarfile.TarFile) -> None:
    tar.size = len(data)
    tar.mtime = int(time.time())
    tar.uid = 0
    tar.gid = 0
    tar.uname = "root"
    tar.gname = "root"
    tar_obj.addfile(tar, io.BytesIO(data))


def build_tar_gz(entries: List[Entry], dirs: List[str]) -> bytes:
    """把目录与文件打成 ``./`` 开头的 tar.gz。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        for d in dirs:
            info = tarfile.TarInfo("./" + d.strip("/") + "/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = int(time.time())
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            tar.addfile(info)
        for e in entries:
            if e.is_dir:
                info = tarfile.TarInfo("./" + e.install_path.strip("/") + "/")
                info.type = tarfile.DIRTYPE
                info.mode = e.mode
                info.mtime = int(time.time())
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                tar.addfile(info)
            else:
                info = tarfile.TarInfo("./" + e.install_path)
                info.mode = e.mode
                _tar_add_file(info, e.read(), tar)
    return buf.getvalue()


def build_control_tar_gz(files: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        for name in ["control", "md5sums", "conffiles", "postinst", "prerm", "postrm"]:
            if name not in files:
                continue
            info = tarfile.TarInfo("./" + name)
            mode = 0o644 if name in ("control", "md5sums", "conffiles") else 0o755
            info.mode = mode
            _tar_add_file(info, files[name], tar)
    return buf.getvalue()


def _ar_member(name: str, data: bytes) -> bytes:
    """按 dpkg-deb 的写法生成一个 ar 成员头（名字补空格到 16 字节）。"""
    header = (
        name.ljust(16)[:16].encode("ascii")
        + str(int(time.time())).ljust(12)[:12].encode("ascii")
        + "0".ljust(6)[:6].encode("ascii")       # uid
        + "0".ljust(6)[:6].encode("ascii")       # gid
        + "100644".ljust(8)[:8].encode("ascii")  # mode
        + str(len(data)).ljust(10)[:10].encode("ascii")
        + b"`\n"
    )
    out = header + data
    if len(data) % 2:  # ar 成员按偶数对齐
        out += b"\n"
    return out


def build_deb(entries: List[Entry], control_files: Dict[str, bytes]) -> bytes:
    data_tar = build_tar_gz(entries, dirs=["usr", "usr/bin", "usr/lib", "usr/lib/radarpi", "usr/share",
                                           "usr/share/doc", "usr/share/doc/radarpi",
                                           "etc", "etc/radarpi", "lib", "lib/systemd", "lib/systemd/system",
                                           "lib/udev", "lib/udev/rules.d", "var", "var/lib"])
    control_tar = build_control_tar_gz(control_files)
    out = b"!<arch>\n"
    out += _ar_member("debian-binary", b"2.0\n")
    out += _ar_member("control.tar.gz", control_tar)
    out += _ar_member("data.tar.gz", data_tar)
    return out


# --------------------------------------------------------------------------
# md5sums / conffiles
# --------------------------------------------------------------------------


def build_md5sums(entries: List[Entry]) -> bytes:
    lines = []
    for e in sorted(entries, key=lambda x: x.install_path):
        if e.is_dir:
            continue
        digest = hashlib.md5(e.read()).hexdigest()
        lines.append("%s  %s" % (digest, e.install_path))
    return ("\n".join(lines) + "\n").encode("utf-8")


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------


def parse_ar(data: bytes) -> List[Tuple[str, bytes]]:
    if not data.startswith(b"!<arch>\n"):
        raise ValueError("不是 ar 归档")
    members: List[Tuple[str, bytes]] = []
    pos = 8
    while pos + 60 <= len(data):
        header = data[pos:pos + 60]
        name = header[0:16].decode("ascii").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError:
            break
        body = data[pos + 60:pos + 60 + size]
        members.append((name, body))
        pos += 60 + size + (size % 2)
    return members


def verify(deb: bytes, version: str) -> List[str]:
    """解析一遍刚生成的包，确认结构与文件都在。"""
    problems: List[str] = []
    members = parse_ar(deb)
    names = [n for n, _ in members]
    if names[:3] != ["debian-binary", "control.tar.gz", "data.tar.gz"]:
        problems.append("ar 成员顺序不对：%s" % names)
    control_tar = dict(members).get("control.tar.gz", b"")
    data_tar = dict(members).get("data.tar.gz", b"")
    for label, blob in (("control.tar.gz", control_tar), ("data.tar.gz", data_tar)):
        if not blob.startswith(b"\x1f\x8b"):
            problems.append("%s 不是 gzip" % label)

    try:
        with tarfile.open(fileobj=io.BytesIO(control_tar)) as tf:
            cnames = tf.getnames()
            control = tf.extractfile("./control").read().decode("utf-8")
    except (tarfile.TarError, AttributeError) as exc:
        problems.append("control.tar.gz 解析失败：%s" % exc)
        return problems
    for need in ("./control", "./md5sums", "./conffiles", "./postinst"):
        if need not in cnames:
            problems.append("control.tar.gz 缺少 %s" % need)
    if ("Version: %s" % version) not in control:
        problems.append("control 里的版本号与源码不一致")
    if "Architecture: all" not in control:
        problems.append("Architecture 应为 all")

    try:
        with tarfile.open(fileobj=io.BytesIO(data_tar)) as tf:
            # tarfile 读取时会把目录名末尾的 "/" 去掉，这里统一归一化后再比对
            dnames = {n.rstrip("/") for n in tf.getnames()}
    except tarfile.TarError as exc:
        problems.append("data.tar.gz 解析失败：%s" % exc)
        return problems

    required = [
        "./usr/bin/radarpi",
        "./usr/lib/radarpi/radarpi/__init__.py",
        "./usr/lib/radarpi/radarpi/protocol.py",
        "./usr/lib/radarpi/radarpi/static/index.html",
        "./usr/lib/radarpi/radarpi/static/app.js",
        "./usr/lib/radarpi/radarpi/static/style.css",
        "./etc/radarpi/radarpi.conf",
        "./lib/systemd/system/radarpi-web.service",
        "./lib/udev/rules.d/99-radarpi.rules",
        "./var/lib/radarpi",
    ]
    for need in required:
        if need not in dnames:
            problems.append("data.tar.gz 缺少 %s" % need)

    # md5sums 与实际内容比对
    sums = dict()
    try:
        with tarfile.open(fileobj=io.BytesIO(control_tar)) as tf:
            for line in tf.extractfile("./md5sums").read().decode("utf-8").splitlines():
                if "  " in line:
                    digest, path = line.split("  ", 1)
                    sums["./" + path] = digest
    except Exception as exc:  # noqa: BLE001
        problems.append("md5sums 解析失败：%s" % exc)
    with tarfile.open(fileobj=io.BytesIO(data_tar)) as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            digest = hashlib.md5(tf.extractfile(member).read()).hexdigest()
            if member.name not in sums:
                problems.append("md5sums 缺少 %s" % member.name)
            elif sums[member.name] != digest:
                problems.append("md5sums 与 %s 不一致" % member.name)
    return problems


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 radarpi 的 .deb 安装包")
    ap.add_argument("--version", help="覆盖版本号")
    ap.add_argument("-o", "--outdir", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--verify", action="store_true", help="打包后自检")
    args = ap.parse_args()

    version = args.version or read_version()
    entries = collect_entries()
    total = sum(len(e.read()) for e in entries if not e.is_dir)
    installed_kb = max(1, (total + 1023) // 1024)

    control_files: Dict[str, bytes] = {
        "control": build_control(version, installed_kb),
        "md5sums": build_md5sums(entries),
        "conffiles": b"/etc/radarpi/radarpi.conf\n",
    }
    for script in ("postinst", "prerm", "postrm"):
        path = os.path.join(HERE, script)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                control_files[script] = fh.read()
        else:
            print("警告：缺少 %s，包里不会有该维护脚本" % script, file=sys.stderr)

    deb = build_deb(entries, control_files)
    os.makedirs(args.outdir, exist_ok=True)
    out = os.path.join(args.outdir, "%s_%s_%s.deb" % (PACKAGE, version, ARCH))
    with open(out, "wb") as fh:
        fh.write(deb)

    print("已生成：%s" % out)
    print("  版本 %s，架构 %s，%d 个文件，%.1f KB 解包后约 %d KB"
          % (version, ARCH, sum(1 for e in entries), len(deb) / 1024.0, installed_kb))
    print("  安装：sudo apt install %s" % os.path.basename(out))

    if args.verify:
        problems = verify(deb, version)
        if problems:
            print("\n自检发现问题：")
            for p in problems:
                print("  - %s" % p)
            return 1
        print("\n自检通过：ar 结构、control、md5sums、必需文件齐全。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
