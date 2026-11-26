# -*- coding: utf-8 -*-
"""gas_sweep.py -- QZKJ 气体传感器全谱探针: 波特率 x 协议 两维扫。
用法: python gas_sweep.py [串口=/dev/ttyS0]
每次尝试: 发问询 -> 等 1.2s -> 收包打印 hex 与解析。哪个组合出有效帧即报。
"""
import struct
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyS0"


def crc16(d):
    c = 0xFFFF
    for b in d:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return bytes([c & 0xFF, (c >> 8) & 0xFF])


def lrc(d):  # 简易协议校验: 取反(Byte1..7 和)+1
    return (0xFF - (sum(d[1:8]) & 0xFF) + 1) & 0xFF


STATUS = {0: "预热", 1: "正常", 2: "数据错误", 3: "传感器故障", 4: "预警",
          5: "低报", 6: "高报", 7: "访问故障", 8: "超量程", 9: "需标定",
          0x0A: "超时", 0x0F: "通信故障"}

frames = []
mb = bytes([1, 3, 0, 0, 0, 10])
frames.append(("Modbus@%d" % 0, 9600, mb + crc16(mb), "modbus"))
sp = bytes([0xFF, 0x01, 0x86, 0, 0, 0, 0, 0])
frames.append(("简易0x86@%d" % 0, 9600, bytes([*sp, lrc(sp)]), "simple"))

s = serial.Serial(PORT, 9600, timeout=1.3)
hit = None
for name, baud, tx, kind in frames:
    if s.baudrate != baud:
        s.baudrate = baud
    s.reset_input_buffer()
    s.write(tx)
    time.sleep(0.1)
    r = s.read(64)
    print("[%s] 发 %s" % (name, tx.hex().upper()))
    print("      收 %s (%d 字节)" % (r.hex().upper() if r else "(空)", len(r)))
    if kind == "modbus" and len(r) == 25 and r[0] == 1 and r[1] == 3 and crc16(r[:23]) == r[23:25]:
        regs = [struct.unpack(">H", r[3 + 2 * k:5 + 2 * k])[0] for k in range(10)]
        dec = (regs[0] >> 8) & 0xF
        conc = regs[1] / (10 ** dec) if dec < 4 else float(regs[1])
        print("      ★ 有效! 浓度=%g 状态=%s 温度=%.1fC 类型=%d 湿度=%.1f%% 量程=%d"
              % (conc, STATUS.get(regs[5] & 0xFF, hex(regs[5])),
                 (regs[7] - 500) / 10.0, regs[8] >> 8, regs[9] / 10.0, regs[4]))
        hit = "modbus"
    elif kind == "simple" and len(r) == 9 and r[0] == 0xFF and r[2] == 0x86:
        conc = r[3] * 256 + r[4]
        print("      ★ 有效! 浓度=%d" % conc)
        hit = "simple"
s.close()
print("结论:", hit or "全无应答(接线/供电问题, 与波特率协议无关)")
