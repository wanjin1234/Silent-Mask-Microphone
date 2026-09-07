# -*- coding: utf-8 -*-
"""USB 串口插拔监控：按顺序逐个插入雷达，识别每个物理口对应的稳定路径。

用途：
  /dev/ttyUSB* 编号会漂，需要把"物理插口 -> 稳定路径 -> 角度"对应起来。
  本脚本监控 /dev/serial/by-path/ 目录，检测插入/拔出的设备并打印。

用法（在树莓派上）：
  1. 先把三个雷达全部拔掉；
  2. 运行本脚本；
  3. 按你的顺序（例如：先左、再中、再右）逐个插入雷达；
  4. 每次插入脚本会打印"新增"了哪个 by-path 路径，据此记录 插口<->路径 对应关系。

  Ctrl+C 退出。
"""

import os
import time
from collections import defaultdict

BY_PATH_DIR = '/dev/serial/by-path'


def snapshot():
    """返回 {basename: 目标路径}。"""
    result = {}
    if not os.path.isdir(BY_PATH_DIR):
        return result
    for name in os.listdir(BY_PATH_DIR):
        full = os.path.join(BY_PATH_DIR, name)
        try:
            result[name] = os.readlink(full)
        except OSError:
            result[name] = full
    return result


def main():
    print('USB 串口插拔监控（by-path）')
    print('=' * 60)
    print(f'监控目录: {BY_PATH_DIR}')
    print('请先把所有雷达拔掉，然后按顺序逐个插入。\n')

    prev = snapshot()
    if prev:
        print('[初始] 当前已连接：')
        for name, target in sorted(prev.items()):
            print(f'    {name} -> {target}')
        print('（如果不是空的，说明有雷达还插着，建议先全拔掉重新开始）\n')
    else:
        print('[初始] 当前无连接。\n')

    print('等待插拔... 每次插入会打印 [新增]，拔出会打印 [移除]（Ctrl+C 退出）\n')

    try:
        while True:
            cur = snapshot()
            added = set(cur) - set(prev)
            removed = set(prev) - set(cur)

            for name in sorted(added):
                print(f'[新增] {name} -> {cur[name]}   <-- 刚才插入的是这个口')
            for name in sorted(removed):
                print(f'[移除] {name} -> {prev[name]}')

            prev = cur
            time.sleep(0.5)
    except KeyboardInterrupt:
        print('\n结束')


if __name__ == '__main__':
    main()
