# -*- coding: utf-8 -*-
# 30s 联动: 板内盯 eth0 carrier + PC 侧连 ping, 用户同时晃动网线插头
import re, subprocess, threading, time
import serial

PORT, USER, PW = "COM4", "pi", "pi"
HOST = "192.168.123.50"
DUR = 32

ping_out = {}
def pc_ping():
    r = subprocess.run(["ping", "-n", str(DUR), "-w", "500", HOST], capture_output=True)
    ping_out["txt"] = r.stdout.decode("gbk", "replace")

ser = serial.Serial(PORT, 115200, timeout=0.2)

def read_until(pat, timeout=8):
    p = re.compile(pat.encode())
    buf = b""
    t0 = time.time()
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

# 登录(可能停在 login, 也可能上次 exit 已回 login)
ser.write(b"\r\n"); time.sleep(0.6)
first = drain(0.8)
if b"login:" in first:
    ser.write(USER.encode() + b"\n")
    r = read_until(r"assword:", 5)
    if b"assword" in r:
        ser.write(PW.encode() + b"\n"); read_until(r"[$#] ", 8)
elif b"assword" in first:
    ser.write(PW.encode() + b"\n"); read_until(r"[$#] ", 8)
drain(0.5)

ser.write(b"\n"); drain(0.4)
watch = ("for i in $(seq 1 %d); do "
         "echo \"W $(date +%%H:%%M:%%S) C=$(cat /sys/class/net/eth0/carrier 2>&1)\"; "
         "sleep 0.5; done" % (DUR - 2))
th = threading.Thread(target=pc_ping); th.start()
t0 = time.time()
ser.write(watch.encode() + b"\n")

lines, last_c, changes = [], None, 0
while time.time() - t0 < DUR:
    c = ser.read(4096)
    if not c: continue
    for ln in c.decode("utf-8", "replace").splitlines():
        ln = ln.strip()
        m = re.match(r"^W (\d\d:\d\d:\d\d) C=(.+)$", ln)
        if m:
            cv = m.group(2)
            if cv != last_c:
                print("  [%s] carrier -> %s" % (m.group(1), cv))
                last_c, changes = cv, changes + 1

th.join()
ser.write(b"\n"); drain(0.3)
ser.write(b"exit\n"); drain(0.4)
ser.close()

print("\n== carrier 变化次数: %d (0=全程死, >=2=闪断/虚接) ==" % max(0, changes - 1))
t = ping_out.get("txt", "")
for l in t.splitlines():
    if "丢失" in l or "往返" in l or "平均" in l:
        print("PC ping:", l.strip())
