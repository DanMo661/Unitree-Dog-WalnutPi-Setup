# -*- coding: utf-8 -*-
# 串口登录板子并执行诊断命令, 从板内看 eth0/PHY 状态
import sys, time, re
import serial

PORT = "COM4"
USER, PW = "pi", "pi"

ser = serial.Serial(PORT, 115200, timeout=0.2)

def read_until(pattern, timeout=10):
    pat = re.compile(pattern.encode() if isinstance(pattern, str) else pattern)
    buf = b""
    t0 = time.time()
    while time.time() - t0 < timeout:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
            if pat.search(buf):
                return buf
    return buf

def drain(t=0.5):
    time.sleep(t)
    out = b""
    while True:
        c = ser.read(4096)
        if not c: break
        out += c
    return out

print("== login ==")
ser.write(b"\r\n")
r = read_until(r"login:", 5)
if b"assword" in r:
    pass  # 意外在密码提示
if b"login:" in r:
    ser.write(USER.encode() + b"\n")
    r2 = read_until(r"assword:", 5)
    if b"assword" in r2:
        ser.write(PW.encode() + b"\n")
        read_until(r"[$#] ", 8)
drain(0.8)
print("login done")

CMDS = [
    ("网络简况", "ip -br addr"),
    ("链路明细", "ip link show eth0; echo CARRIER=$(cat /sys/class/net/eth0/carrier 2>&1) OPERSTATE=$(cat /sys/class/net/eth0/operstate 2>&1)"),
    ("PHY dmesg", "dmesg | grep -iE 'phy|eth0|emac' | tail -12"),
    ("bridge服务", "systemctl is-active go2-bridge; uptime | tail -1"),
    ("ping PC", "ping -c 3 -W 1 192.168.123.137 2>&1 | tail -2"),
]

for name, cmd in CMDS:
    ser.write(b"\n")
    drain(0.4)
    ser.write(cmd.encode() + b"\n")
    out = read_until(r"[$#] $", 8).decode("utf-8", "replace")
    # 去掉回显的命令行本身
    lines = [l for l in out.splitlines() if l.strip()]
    print("\n== %s ==" % name)
    for l in lines[1:-1]:
        print("  " + l)

ser.write(b"\n")
drain(0.3)
ser.write(b"exit\n")
drain(0.5)
ser.close()
print("\ndone")
