# -*- coding: utf-8 -*-
"""指定 GPIO 脚位每秒翻转电平 —— 接线/继电器 bring-up 排障用(2026-10-29)。
板上运行: sudo python3 pin_blink.py [gpio编号 ...]   # 缺省 PI15/PI16/PC8 = 271 272 72
停止: sudo pkill -f pin_blink.py (SIGTERM 自动 unexport 复位脚位)
"""
import os
import signal
import sys
import time

PINS = [int(a) for a in sys.argv[1:]] or [271, 272, 72]   # PI15, PI16, PC8


def unexport():
    for p in PINS:
        try:
            with open("/sys/class/gpio/unexport", "w") as f:
                f.write(str(p))
        except OSError:
            pass


def stop(_sig, _frm):
    unexport()
    sys.exit(0)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

for p in PINS:
    if not os.path.exists("/sys/class/gpio/gpio%d" % p):
        with open("/sys/class/gpio/export", "w") as f:
            f.write(str(p))
    with open("/sys/class/gpio/gpio%d/direction" % p, "w") as f:
        f.write("out")

level = 0
while True:
    for p in PINS:
        with open("/sys/class/gpio/gpio%d/value" % p, "w") as f:
            f.write(str(level))
    print(time.strftime("%H:%M:%S"), "pins", PINS, "->", level, flush=True)
    level ^= 1
    time.sleep(1)
