# -*- coding: utf-8 -*-
"""第一步: SBUS 通道监视器(不连狗), 用来实测各通道再填 config.py
用法: .venv/bin/python sbus_monitor.py [串口, 默认 config.SBUS_PORT(核桃派=/dev/ttyS2)]
正常: 搬动摇杆/拨开关, 实时打印通道原始值(行程 172~1811, 中值 992); 关遥控器 -> 失控:1
"""
import sys
import time

import config
from sbus import Sbus

port = sys.argv[1] if len(sys.argv) > 1 else config.SBUS_PORT
s = Sbus(port)
print(f"打开 {port} ... (遥控器开机、接收机通电)")
last, last_print = None, 0.0
while True:
    if not s.opened:
        s.open()
        if not s.opened:
            if time.time() - last_print > 3:
                print(f"打不开 {port}: 检查接线/串口号 (ls /dev/ttyS* 看可用串口, config.SBUS_PORT 可改)")
                last_print = time.time()
            time.sleep(3)
            continue
    f = s.read()
    if f is None:
        if time.time() - last_print > 3:
            print("无有效帧: 查 1)串口号 2)TX/RX 是否接反 3)SBUS 是否做了反相 4)接收机是否通电")
            last_print = time.time()
        continue
    ch, failsafe, lost = f
    now = time.time()
    if ch != last and now - last_print > 0.1:
        print(" ".join(f"{v:4d}" for v in ch[:10]), "| 失控:", int(failsafe), "丢帧:", int(lost))
        last, last_print = list(ch), now
