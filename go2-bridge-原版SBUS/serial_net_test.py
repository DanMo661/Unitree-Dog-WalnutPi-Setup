# -*- coding: utf-8 -*-
# 一把测: ping 板子 + CH340 串口监听20s(每2.5s敲回车), 全部字节计数+预览
import subprocess, sys, time
import serial
from serial.tools import list_ports

HOST = "192.168.123.50"

print("== 1. ping %s ==" % HOST)
r = subprocess.run(["ping", "-n", "2", "-w", "1000", HOST], capture_output=True)
out = r.stdout.decode("gbk", "replace")
ok = "TTL=" in out.upper()
print("ping =>", "OK" if ok else "FAIL")
if not ok:
    print(out.strip().splitlines()[-1] if out.strip() else "(no output)")
arp = subprocess.run(["arp", "-a", HOST], capture_output=True).stdout.decode("gbk", "replace")
print("arp  =>", arp.strip().splitlines()[-1] if arp.strip() else "(empty)")

print("\n== 2. COM ports ==")
ports = list_ports.comports()
ch340 = None
for p in ports:
    print("  %s  %s  [%s]" % (p.device, p.description, p.hwid))
    if "CH340" in (p.description or ""):
        ch340 = p.device
if not ch340:
    print("!! no CH340 found")
    sys.exit(1)

print("\n== 3. listen %s @115200 for 20s (poke CRLF every 2.5s) ==" % ch340)
ser = serial.Serial(ch340, 115200, timeout=0.3)
buf = b""
t0 = time.time()
next_poke = 0.0
while time.time() - t0 < 20:
    if time.time() - t0 >= next_poke:
        ser.write(b"\r\n")
        next_poke += 2.5
    chunk = ser.read(4096)
    if chunk:
        buf += chunk
        sys.stdout.write(chunk.decode("utf-8", "replace"))
        sys.stdout.flush()
ser.close()
print("\n---- total %d bytes ----" % len(buf))
if buf:
    print("first 300 bytes repr: %r" % buf[:300])
