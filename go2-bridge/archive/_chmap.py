# -*- coding: utf-8 -*-
"""临时诊断: 15秒采样 /ws 全通道取值范围(用户翻开关时抓映射)"""
import json
import time
import websocket

ws = websocket.create_connection("ws://127.0.0.1:8000/ws", timeout=5)
ws.settimeout(1)
lo = [9999] * 16
hi = [-1] * 16
t0 = time.time()
n = 0
while time.time() - t0 < 15:
    try:
        d = json.loads(ws.recv())
    except Exception:
        continue
    ch = d.get("ch")
    if not ch:
        continue
    n += 1
    for i, v in enumerate(ch[:16]):
        lo[i] = min(lo[i], v)
        hi[i] = max(hi[i], v)
print("samples", n)
for i in range(16):
    if lo[i] <= hi[i]:
        print("ch%-2d  %5d ~ %5d %s" % (i, lo[i], hi[i], "<-- 有变化" if hi[i] - lo[i] > 50 else ""))
ws.close()
