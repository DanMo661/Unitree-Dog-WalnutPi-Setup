# -*- coding: utf-8 -*-
"""逐脚试灯: 依次把 35/37/40 脚拉高 3 秒, 观察继电器/灯跟哪次动, 定位引线.
用法: sudo python3 gpio_wiggle.py [引脚名]  例: sudo python3 gpio_wiggle.py PI14
不带参数=依次试 35(PI3) 37(PI4) 40(PI14) 三个脚各一轮."""
import sys
import time

PINS = {"PI3": (35, 259), "PI4": (37, 260), "PI14": (40, 270)}


def pulse(name):
    phys, n = PINS[name]
    d = "/sys/class/gpio/gpio%d" % n
    try:
        open(d + "/value")
    except OSError:
        open("/sys/class/gpio/export", "w").write(str(n))
    open(d + "/direction", "w").write("out")
    print(">> %s脚(%s) = 高 3秒" % (phys, name), flush=True)
    open(d + "/value", "w").write("1")
    time.sleep(3)
    open(d + "/value", "w").write("0")
    print("   已拉低", flush=True)
    time.sleep(1)


if len(sys.argv) > 1:
    pulse(sys.argv[1])
else:
    for k in ("PI3", "PI4", "PI14"):
        pulse(k)
print("试灯完成")
