# -*- coding: utf-8 -*-
"""通道标定: 采样 N 秒, 输出每通道 min/max、帧率、最大帧间隔, 结果用于回填 config.py
用法: .venv/bin/python calibrate.py [秒数, 默认60] [串口, 默认 config.SBUS_PORT]
采样期间: 左摇杆 前后/左右、右摇杆 左右、三段开关三档、解锁开关两档, 逐个拨到位各停半秒
"""
import sys
import time

import config
from sbus import Sbus

secs = float(sys.argv[1]) if len(sys.argv) > 1 else 60
port = sys.argv[2] if len(sys.argv) > 2 else config.SBUS_PORT
s = Sbus(port)
if not s.open():
    raise SystemExit("打不开 " + port)
lo, hi = [9999] * 16, [-1] * 16
n = 0
last_print = time.time()
last_frame = None
max_gap = 0.0
t0 = time.time()
print(f"采样 {secs:.0f}s, 开始逐个动摇杆/拨开关 (每个方向停半秒) ...")
while time.time() - t0 < secs:
    f = s.read()
    now = time.time()
    if f is None:
        if last_frame is not None and now - last_frame > max_gap:
            max_gap = now - last_frame
        continue
    if last_frame is not None and now - last_frame > max_gap:
        max_gap = now - last_frame
    last_frame = now
    ch, fs, lost = f
    n += 1
    for i, v in enumerate(ch):
        if v < lo[i]:
            lo[i] = v
        if v > hi[i]:
            hi[i] = v
    if now - last_print >= 5:
        last_print = now
        print(f"  {now - t0:.0f}s 累计 {n} 帧")
print(f"\n共 {n} 帧, 平均 {n / secs:.0f} 帧/s (正常约140), 最大帧间隔 {max_gap:.2f}s")
print("CH   min  max  span")
for i in range(16):
    if hi[i] >= 0:
        mark = "  <-- 有动作" if hi[i] - lo[i] > 150 else ""
        print(f"{i:2d}  {lo[i]:4d} {hi[i]:4d}  {hi[i] - lo[i]:4d}{mark}")
