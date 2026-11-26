# -*- coding: utf-8 -*-
"""板端诊断: 用和服务完全相同的代码路径跑探测, 看 _path_ok 到底卡在哪一步"""
import asyncio
import socket
import sys
import time

sys.path.insert(0, "/home/pi/go2-bridge")
import bridge
import config

print("ROBOT_IP=%s SIGNAL_PORT=%d ETH_IFACE=%s" % (
    config.ROBOT_IP, config.SIGNAL_PORT, config.ETH_IFACE))

# 1) 载波
try:
    with open("/sys/class/net/%s/carrier" % config.ETH_IFACE) as f:
        print("carrier = %r" % f.read())
except OSError as e:
    print("carrier ERR:", e)

# 2) 裸 TCP 探测 5 次(和服务同参数)
for i in range(5):
    t0 = time.monotonic()
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect((config.ROBOT_IP, config.SIGNAL_PORT))
        r = "OK"
    except OSError as e:
        r = repr(e)
    finally:
        s.close()
    print("tcp %d: %s (%.2fs)" % (i, r, time.monotonic() - t0))


# 3) 经 run_in_executor 跑真 _path_ok (服务的完整路径)
async def t():
    for i in range(5):
        t0 = time.monotonic()
        ok = await asyncio.get_running_loop().run_in_executor(None, bridge._path_ok)
        print("path_ok(executor) %d: %s (%.2fs)" % (i, ok, time.monotonic() - t0))
        await asyncio.sleep(2)

asyncio.run(t())
