# -*- coding: utf-8 -*-
"""临时诊断: AF_PACKET 抓 eth0 12秒, 按源MAC+源IP汇总(修正版: ethertype 0x0800)"""
import socket
import struct
import time

s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
s.settimeout(1)
seen = {}
end = time.time() + 12
while time.time() < end:
    try:
        d = s.recv(2048)
    except socket.timeout:
        continue
    _, src, proto = struct.unpack("!6s6sH", d[:14])
    mac = ":".join("%02x" % b for b in src)
    sip = dip = ""
    if proto == 0x0800 and len(d) >= 38:
        sip = ".".join(str(b) for b in d[26:30])
        dip = ".".join(str(b) for b in d[30:34])
    k = (mac, sip)
    e = seen.setdefault(k, {"n": 0, "protos": set(), "dips": set()})
    e["n"] += 1
    e["protos"].add(proto)
    if dip:
        e["dips"].add(dip)
for (mac, sip), e in sorted(seen.items(), key=lambda kv: -kv[1]["n"]):
    print(mac, sip or "-", e["n"], sorted(e["protos"]), sorted(e["dips"])[:4])
