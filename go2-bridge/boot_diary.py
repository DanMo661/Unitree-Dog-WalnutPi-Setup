# -*- coding: utf-8 -*-
"""开机链路会诊: 单调时钟给每个里程碑打点, 下次开机谁慢一眼定位
看结果: journalctl -t boot-diary -o short-monotonic
打点: 会诊开始 / 网线载波起落 / 狗开始应ping / 狗信令口9991就绪(桥此时才能发起连接)"""
import socket
import subprocess
import time

T0 = time.monotonic()
DOG_IP, DOG_PORT = "192.168.123.161", 9991


def stamp(msg):
    subprocess.call(["logger", "-t", "boot-diary", "[%6.1fs] %s" % (time.monotonic() - T0, msg)])


def ping_dog():
    r = subprocess.run(["ping", "-c", "1", "-W", "1", DOG_IP],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def tcp_dog():
    s = socket.socket()
    s.settimeout(0.8)
    try:
        s.connect((DOG_IP, DOG_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


stamp("会诊开始 (boot_diary 拉起)")
st = {"carrier": None, "ping": False, "tcp": False}
while True:
    try:
        with open("/sys/class/net/eth0/carrier") as f:
            c = f.read().strip()
    except OSError:
        c = "?"
    if c != st["carrier"]:
        stamp("网线载波: %s" % ("up" if c == "1" else c))
        st["carrier"] = c

    p = ping_dog() if c == "1" else False
    if p and not st["ping"]:
        stamp("狗开始应答 ping")
    if not p and st["ping"]:
        stamp("狗 ping 失去")
    st["ping"] = p

    t = tcp_dog() if p else False
    if t and not st["tcp"]:
        stamp("狗信令口 9991 就绪 <- 桥此刻才能发起连接")
    if not t and st["tcp"]:
        stamp("狗信令口 9991 失去")
    st["tcp"] = t

    time.sleep(0.5)
