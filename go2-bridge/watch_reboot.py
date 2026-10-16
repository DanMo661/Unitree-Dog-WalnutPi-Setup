# -*- coding: utf-8 -*-
"""整机重启会诊: PC 侧盯 123.50 断电->回网, 板侧拉 boot-diary + 桥连接时间线"""
import subprocess
import time

import paramiko

HOST = "192.168.123.50"
T0 = time.time()


def now():
    return "[+%5.1fs]" % (time.time() - T0)


def ping():
    r = subprocess.run(["ping", "-n", "1", "-w", "700", HOST],
                       capture_output=True)
    return "TTL=" in r.stdout.decode("gbk", "replace").upper()


def log(msg):
    print("%s %s" % (now(), msg), flush=True)


# Phase 0: 等断电(最多 240s; 当前本来就不通就直接进 Phase 1)
log("开始监听, 等你断电...")
was_up = ping()
if was_up:
    for i in range(480):
        time.sleep(0.5)
        if not ping():
            log("板子断电 ✓")
            break
    else:
        log("240s 没等到断电, 放弃")
        raise SystemExit(1)

# Phase 1: 等回网
log("等板子回网...")
t_off = time.time()
for i in range(300):
    time.sleep(1)
    if ping():
        break
else:
    log("300s 板子没回网!")
    raise SystemExit(1)
log("板子回网 ✓ (断电 %.0fs)" % (time.time() - t_off))

# Phase 2: 等 SSH
c = None
for i in range(60):
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


# Phase 3: 等桥连上狗(最多 420s, 每 5s 查一次)
log("等桥连狗(最多420s)...")
for i in range(84):
    time.sleep(5)
    up = run("echo pi | sudo -S journalctl -u go2-bridge -b --no-pager -o short-monotonic 2>/dev/null "
             "| grep -ac 'WebRTC 已连接' || true")
    if up.strip().isdigit() and int(up.strip()) > 0:
        log("桥已连上狗 ✓ (回网后 %.0fs)" % (time.time() - t_off))
        break
else:
    log("420s 桥还没连上狗!")

time.sleep(3)

# Phase 4: 全套时间线
print("\n================ 会诊结果 ================", flush=True)
print("---- boot-diary (单调时钟) ----", flush=True)
print(run("echo pi | sudo -S journalctl -b -t boot-diary --no-pager -o short-monotonic 2>/dev/null | head -20"))
print("---- 桥关键行 (单调时钟) ----", flush=True)
print(run("echo pi | sudo -S journalctl -b -u go2-bridge --no-pager -o short-monotonic 2>/dev/null "
          "| grep -aE 'go2-bridge 启动|SBUS 串口|舵机|WebRTC 已连接|链路不通|会话结束|复位' | head -20"))
print("---- 内核网卡 ----", flush=True)
print(run("echo pi | sudo -S journalctl -b -k --no-pager -o short-monotonic 2>/dev/null "
          "| grep -aiE 'link is up|phy init|PHY ID' | head -5"))
print("---- uptime ----", flush=True)
print(run("uptime -s; uptime"))
c.close()
print("\n==== 会诊结束 ====", flush=True)
