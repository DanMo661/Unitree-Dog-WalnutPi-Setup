# -*- coding: utf-8 -*-
"""长监听: 板子回网即自动拉 boot-diary + 桥时间线(最多 20 分钟)"""
import subprocess
import time

import paramiko

HOST = "192.168.123.50"
T0 = time.time()


def now():
    return "[+%5.1fs]" % (time.time() - T0)


def log(msg):
    print("%s %s" % (now(), msg), flush=True)


def ping():
    r = subprocess.run(["ping", "-n", "1", "-w", "700", HOST], capture_output=True)
    return "TTL=" in r.stdout.decode("gbk", "replace").upper()


log("长监听开始, 等板子回网(最多1200s)...")
t_first = None
for i in range(600):
    time.sleep(2)
    if ping():
        t_first = time.time()
        log("板子回网 ✓")
        break
    if i % 30 == 29:
        log("...仍在等 (%.0fs)" % (time.time() - T0))
if not t_first:
    log("20分钟没回网, 放弃")
    raise SystemExit(1)

c = None
for i in range(40):
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(HOST, username="pi", password="pi", timeout=5)
        break
    except Exception:
        time.sleep(2)
if not c:
    log("SSH 上不去")
    raise SystemExit(1)
log("SSH 通 ✓")


def run(cmd, t=30):
    _, o, e = c.exec_command(cmd, timeout=t)
    return (o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")).strip()


for i in range(84):
    time.sleep(5)
    up = run("echo pi | sudo -S journalctl -u go2-bridge -b --no-pager -o short-monotonic 2>/dev/null "
             "| grep -ac 'WebRTC 已连接' || true")
    if up.strip().isdigit() and int(up.strip()) > 0:
        log("桥已连上狗 ✓ (回网后 %.0fs)" % (time.time() - t_first))
        break
else:
    log("420s 桥还没连上狗!")
time.sleep(3)

print("\n================ 会诊结果 ================", flush=True)
print("---- boot-diary (单调时钟, 本次开机起) ----", flush=True)
print(run("echo pi | sudo -S journalctl -b -t boot-diary --no-pager -o short-monotonic 2>/dev/null | head -20"))
print("---- 桥关键行 ----", flush=True)
print(run("echo pi | sudo -S journalctl -b -u go2-bridge --no-pager -o short-monotonic 2>/dev/null "
          "| grep -aE 'go2-bridge 启动|SBUS 串口|舵机|WebRTC 已连接|链路不通|会话结束|复位' | head -20"))
print("---- 内核网卡 ----", flush=True)
print(run("echo pi | sudo -S journalctl -b -k --no-pager -o short-monotonic 2>/dev/null "
          "| grep -aiE 'link is up|phy init|PHY ID' | head -5"))
print("---- uptime ----", flush=True)
print(run("uptime -s; uptime"))
c.close()
print("\n==== 会诊结束 ====", flush=True)
