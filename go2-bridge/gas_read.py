# -*- coding: utf-8 -*-
"""gas_read.py -- QZKJ 气体传感器 Modbus-RTU 读取探针(板上跑)。
用法: python gas_read.py [串口=/dev/ttyS0] [轮数=6]
标准帧: 01 03 00 00 00 0A + CRC16, 应答 25 字节(10 寄存器大端)。
"""
import struct
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyS0"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 6


def crc16(d):
    c = 0xFFFF
    for b in d:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return bytes([c & 0xFF, (c >> 8) & 0xFF])


STATUS = {0: "预热", 1: "正常", 2: "数据错误", 3: "传感器故障", 4: "预警",
          5: "低报", 6: "高报", 7: "访问故障", 8: "超量程", 9: "需标定",
          0x0A: "超时", 0x0F: "通信故障"}

s = serial.Serial(PORT, 9600, bytesize=8, parity="N", stopbits=1, timeout=1.5)
q = bytes([1, 3, 0, 0, 0, 10])
q += crc16(q)
print("查询帧:", q.hex().upper(), "端口", PORT)

ok = 0
for i in range(ROUNDS):
    s.reset_input_buffer()
    s.write(q)
    r = s.read(25)
    if len(r) == 25 and r[0] == 1 and r[1] == 3 and crc16(r[:23]) == r[23:25]:
        regs = [struct.unpack(">H", r[3 + 2 * k:5 + 2 * k])[0] for k in range(10)]
        dec = (regs[0] >> 8) & 0xF
        conc = regs[1] / (10 ** dec) if dec < 4 else float(regs[1])
        print("#%d 寄存器: %s" % (i, " ".join("%04X" % x for x in regs)))
        print("   浓度=%g 状态=%s 温度=%.1fC 气体类型=%d 湿度=%.1f%% 量程=%d 低报=%d 高报=%d"
              % (conc, STATUS.get(regs[5] & 0xFF, "0x%02X" % (regs[5] & 0xFF)),
                 (regs[7] - 500) / 10.0, regs[8] >> 8, regs[9] / 10.0,
                 regs[4], regs[2], regs[3]))
        ok += 1
    else:
        print("#%d 无有效帧: %s" % (i, r.hex().upper() if r else "(空)"))
    time.sleep(2)

print("结果: %d/%d 帧有效" % (ok, ROUNDS))
