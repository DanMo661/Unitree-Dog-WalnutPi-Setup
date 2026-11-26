# -*- coding: utf-8 -*-
"""uart_selftest.py -- 纯软件验证 UART0: 内部 loopback + 真·多波特率扫描。
1) loopback: 置 16550 MCR.LOOP 位, 自己发自己收 -- 不碰任何外部接线, 证明 UART 收发硬件正常
2) sweep: 4800/9600/19200/38400/57600/115200 发 Modbus 问询, 看谁有回应
用法: python uart_selftest.py [串口=/dev/ttyS0]
"""
import fcntl
import struct
import sys
import termios
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyS0"
TIOCM_LOOP = getattr(termios, "TIOCM_LOOP", 0x8000)


def crc16(d):
    c = 0xFFFF
    for b in d:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return bytes([c & 0xFF, (c >> 8) & 0xFF])


print("=== 1. 内部 loopback 自检(不需要任何外接线) ===")
s = serial.Serial(PORT, 9600, timeout=1.0)
fd = s.fileno()
s.reset_input_buffer()
fcntl.ioctl(fd, termios.TIOCMBIS, struct.pack("I", TIOCM_LOOP))
time.sleep(0.15)
s.reset_input_buffer()
payload = b"LOOPBACK-OK-12345"
s.write(payload)
time.sleep(0.25)
r = s.read(128)
fcntl.ioctl(fd, termios.TIOCMBIC, struct.pack("I", TIOCM_LOOP))
print("  发出:", payload)
print("  收回:", r)
print("  判定:", "UART 收发硬件正常 ✓" if payload in r else "!! UART 自环失败, 板子侧问题")
s.close()
time.sleep(0.2)

print("=== 2. 多波特率扫描(传感器接线保持现状) ===")
q = bytes([1, 3, 0, 0, 0, 10])
q += crc16(q)
for baud in (4800, 9600, 19200, 38400, 57600, 115200):
    s = serial.Serial(PORT, baud, timeout=1.2)
    time.sleep(0.1)
    s.reset_input_buffer()
    s.write(q)
    time.sleep(0.15)
    r = s.read(64)
    s.close()
    note = ""
    if len(r) == 25 and r[0] == 1 and r[1] == 3 and crc16(r[:23]) == r[23:25]:
        regs = [struct.unpack(">H", r[3 + 2 * k:5 + 2 * k])[0] for k in range(10)]
        dec = (regs[0] >> 8) & 0xF
        conc = regs[1] / (10 ** dec) if dec < 4 else float(regs[1])
        note = " ★★★ Modbus 有效应答! 浓度=%g 温度=%.1fC 状态=0x%02X 类型=%d 湿度=%.1f%%" % (
            conc, (regs[7] - 500) / 10.0, regs[5] & 0xFF, regs[8] >> 8, regs[9] / 10.0)
    print("  %6d: %d 字节 %s%s" % (baud, len(r), r[:24].hex().upper() if r else "(空)", note))
print("=== 完 ===")
