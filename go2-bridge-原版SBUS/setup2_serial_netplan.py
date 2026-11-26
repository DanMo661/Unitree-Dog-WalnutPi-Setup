# -*- coding: utf-8 -*-
"""板#2 (新核桃派) 串口首次配网: 找 CH340 -> 等 login -> 登 pi/pi -> 写 netplan 静态IP -> apply
用法: python setup2_serial_netplan.py [等待上电秒数, 默认180]
板#1 必须断电(同 IP 会冲突)!
"""
import re, sys, time
import serial
from serial.tools import list_ports

NETPLAN = """network:
  version: 2
  ethernets:
    eth0:
      dhcp4: false
      addresses:
        - 192.168.123.50/24
        - 192.168.144.10/24
"""

WAIT_BOOT = int(sys.argv[1]) if len(sys.argv) > 1 else 180

# 1. 找 CH340
port = None
for i in range(10):
    for p in list_ports.comports():
        if "CH340" in (p.description or ""):
            port = p.device
            break
    if port:
        break
    print("等 CH340 插入... (%d/10)" % (i + 1))
    time.sleep(3)
assert port, "没找到 CH340"
print("串口:", port)

ser = serial.Serial(port, 115200, timeout=0.3)
log_all = b""

def wait_for(pat, timeout):
    """等到模式出现, 返回累计缓冲"""
    global log_all
    p = re.compile(pat.encode())
    t0 = time.time()
    while time.time() - t0 < timeout:
        c = ser.read(4096)
        if c:
            log_all += c
            sys.stdout.write(c.decode("utf-8", "replace"))
            sys.stdout.flush()
            if p.search(log_all[-4000:]):
                return True
    return False

def drain(t=0.5):
    out = b""
    time.sleep(t)
    while True:
        c = ser.read(4096)
        if not c:
            break
        out += c
    return out

# 2. 等上电出 login (板子可能还没开, 敲回车促进 getty 吐提示)
print("\n-- 等板子上电 (最长 %ds), 你现在可以插电 --" % WAIT_BOOT)
got = False
t0 = time.time()
while time.time() - t0 < WAIT_BOOT:
    ser.write(b"\r\n")
    if wait_for(r"login:", 5):
        got = True
        break
assert got, "等不到 login 提示, 检查板子供电/TXD-RXD/GND"

# 3. 登录
ser.write(b"pi\n")
wait_for(r"assword:", 8)
ser.write(b"pi\n")
time.sleep(2)
drain(1.0)
print("\n-- 登录完成 --")

# 4. 写 netplan (sudo tee 逐行, 避开引号地狱: 先传 base64)
import base64
b64 = base64.b64encode(NETPLAN.encode()).decode()
def run(cmd, mark, timeout=30):
    ser.write(b"\n"); drain(0.3)
    ser.write(cmd.encode() + b"\n")
    if wait_for(mark, timeout):
        print("  [OK] " + cmd[:60])
    else:
        print("  [!!] 超时: " + cmd[:60])
    drain(0.5)

run("echo %s | base64 -d > /tmp/np.yaml && wc -l /tmp/np.yaml" % b64, r"/tmp/np.yaml", 10)
run("cat /tmp/np.yaml", r"addresses:", 8)
run("echo pi | sudo -S cp /tmp/np.yaml /etc/netplan/01-network-manager-all.yaml && echo CPY_$?", r"CPY_0", 12)
run("echo pi | sudo -S chmod 600 /etc/netplan/01-network-manager-all.yaml", r"[$#] ", 10)
run("echo pi | sudo -S netplan apply && echo NP_$?", r"NP_0", 25)
time.sleep(3)
run("ip -4 addr show eth0", r"inet ", 8)
run("python3 -c 'import venv; print(\"VENV_OK\")' 2>&1 || echo VENV_MISSING", r"VENV_", 10)
run("ping -c 2 -W 1 192.168.123.137 2>&1 | tail -2", r"packet loss|100% loss|0% loss", 8)

ser.write(b"\n"); drain(0.3)
ser.write(b"exit\n"); drain(0.4)
ser.close()
print("\n==== 串口配网结束, 看上面: inet 192.168.123.50 出现 + VENV_OK/MISSING + ping PC 结果 ====")
