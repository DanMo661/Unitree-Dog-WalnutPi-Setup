# -*- coding: utf-8 -*-
# eth0 down/up 复位 PHY, 排除驱动卡死; 仍无载波=物理层问题
import re, time
import serial

PORT, USER, PW = "COM4", "pi", "pi"
ser = serial.Serial(PORT, 115200, timeout=0.2)

def read_until(pat, timeout=8):
    p = re.compile(pat.encode()); buf = b""; t0 = time.time()
    while time.time() - t0 < timeout:
        c = ser.read(4096)
        if c:
            buf += c
            if p.search(buf): return buf
    return buf

def drain(t=0.4):
    time.sleep(t); out = b""
    while True:
        c = ser.read(4096)
        if not c: break
        out += c
    return out

ser.write(b"\r\n"); time.sleep(0.5); first = drain(0.8)
if b"login:" in first:
    ser.write(USER.encode() + b"\n")
    r = read_until(r"assword:", 5)
    if b"assword" in r:
        ser.write(PW.encode() + b"\n"); read_until(r"[$#] ", 8)
drain(0.5)

def run(cmd, wait=6):
    ser.write(b"\n"); drain(0.3)
    ser.write(cmd.encode() + b"\n")
    out = read_until(r"[$#] $", wait).decode("utf-8", "replace")
    return "\n".join(l for l in out.splitlines() if l.strip())

print(run("ip link set eth0 down && sleep 1 && ip link set eth0 up && echo RESET_OK"))
print(run("sleep 4; echo CARRIER=$(cat /sys/class/net/eth0/carrier 2>&1) STATE=$(cat /sys/class/net/eth0/operstate)", 10))
print(run("dmesg | grep -iE 'eth0|phy' | tail -4"))
print(run("which ethtool >/dev/null && ethtool eth0 2>&1 | grep -E 'Link|Speed|Duplex' || echo no-ethtool"))

ser.write(b"\n"); drain(0.3)
ser.write(b"exit\n"); drain(0.4)
ser.close()
print("done")
