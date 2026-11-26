# -*- coding: utf-8 -*-
"""第一步测试: 只验证SBUS, 不连狗
用法: python3 sbus_test.py [串口设备]     例: python3 sbus_test.py /dev/ttyUSB0

正常现象: 遥控器开机、接收机通电后, 搬动摇杆/拨动开关, 屏幕实时打印16个通道值
         (行程约172~1811, 松开摇杆回中约992); 关掉遥控器应看到失控标志变1
"""
import sys
import time

from sbus import Sbus

port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
print(f"打开串口 {port} ... (遥控器开机、接收机通电)")
s = Sbus(port)
last, last_mark = None, time.time()
while True:
    f = s.read()
    if f is None:
        if time.time() - last_mark > 2:
            print("尚未收到有效帧: 检查 1)串口号是否对(ls /dev/ttyUSB*) "
                  "2)TX/RX是否接反 3)SBUS信号是否做了反相 4)接收机是否通电")
            last_mark = time.time()
        continue
    ch, failsafe, lost = f
    if ch != last:
        print(" ".join(f"{v:4d}" for v in ch[:12]), "| 失控:", int(failsafe), "丢帧:", int(lost))
        last = list(ch)
    last_mark = time.time()
