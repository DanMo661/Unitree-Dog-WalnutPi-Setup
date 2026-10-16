# -*- coding: utf-8 -*-
"""板端喊话协议 v3 自测(PC 模拟 App 全行为):
LIST / 上传(故意丢块验证累计确认重传) / PLAY/STOP / TALK 丢包统计 / RATE / REN / DEL
用法: BRIDGE_HOST=192.168.123.50 python test_pa_v3.py
⚠ 这是【板端集成测试, 需板子在线】, 离线跑必然全 FAIL; HOST 缺省 127.0.0.1 防误发真网
"""
import math
import os
import socket
import struct
import sys
import time
import zlib

HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")   # 需真板时显式传 BRIDGE_HOST, 缺省回环防误发
CTRL, AUDIO, UP = 15001, 15000, 15003
CHUNK = 1024
FAILS = []


def ok(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


def ctrl(cmd, timeout=1.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(cmd.encode("utf-8"), (HOST, CTRL))
        d, _ = s.recvfrom(2048)
        return d.decode("utf-8")
    except socket.timeout:
        return None
    finally:
        s.close()


def recvs(s):
    try:
        d, _ = s.recvfrom(512)
        return d.decode("utf-8")
    except socket.timeout:
        return None


def list_files():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.2)
    s.sendto(b"LIST", (HOST, CTRL))
    parts, total, t0 = {}, -1, time.time()
    while time.time() - t0 < 2.5 and (total < 0 or len(parts) < total):
        try:
            d, _ = s.recvfrom(1500)
        except socket.timeout:
            break
        line = d.decode("utf-8")
        if not line.startswith("LST "):
            continue
        nl = line.index("\n")
        k, n = line[4:nl].split("/")
        total, parts[int(k)] = int(n), line[nl + 1:]
    s.close()
    out = []
    for i in range(1, total + 1):
        for ln in parts.get(i, "").split("\n"):
            if ln.strip():
                out.append(ln)
    return out


def make_pcm(seconds=3.0, rate=16000):
    ba = bytearray()
    for i in range(int(seconds * rate)):
        env = 0.6 * (0.5 + 0.5 * math.sin(2 * math.pi * 2 * i / rate))
        ba += struct.pack("<h", int(20000 * env * math.sin(2 * math.pi * 440 * i / rate)))
    return bytes(ba)


def upload(name, data, skip_chunks=0):
    """skip_chunks>0 时故意不发送部分块, 验证累计确认+重传能补齐"""
    crc = zlib.crc32(data) & 0xFFFFFFFF
    sid = "%x" % (int(time.time() * 1000) & 0xFFFF)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    dst = (HOST, UP)
    try:
        s.sendto(("UPB %s %s %d %08x" % (sid, name, len(data), crc)).encode(), dst)
        r = recvs(s)
        if not r or not r.startswith("OK"):
            print("  UPB 应答:", r)
            return False
        off, base, skipped, retries = 0, 0, 0, 0
        t0 = time.time()
        while base < len(data):
            while off < len(data) and off - base < CHUNK * 8:
                if skipped < skip_chunks and (off // CHUNK) % 5 == 2:
                    off += CHUNK
                    skipped += 1          # 模拟这一块丢了
                    continue
                s.sendto(b"UPC %s %d " % (sid.encode(), off) + data[off:off + CHUNK], dst)
                off += CHUNK
            ack = recvs(s)
            if ack and ack.startswith("ACK"):
                base = int(ack.split(" ")[2])
                retries = 0
            else:
                retries += 1
                off = base                # 从未确认前缀重发
                if retries > 40:
                    print("  重传超限")
                    return False
        s.settimeout(4.0)
        s.sendto(("UPE %s" % sid).encode(), dst)
        end = recvs(s)
        dt = time.time() - t0
        print("  上传 %dB 用时 %.1fs (%.0f KB/s), UPE -> %s" % (
            len(data), dt, len(data) / 1024.0 / max(0.1, dt), end))
        return end is not None and end.startswith("DONE")
    finally:
        s.close()


def talk_burst(n, sent_claim):
    print(" ", ctrl("TALK 1"))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pkt = b"\x10\x20" * 640               # 40ms@16k
    for _ in range(n):
        s.sendto(pkt, (HOST, AUDIO))
        time.sleep(0.04)
    s.close()
    return ctrl("TALK 0 %d" % sent_claim)


def main():
    print("== 1. 探活/初始 LIST ==")
    r = ctrl("HELLO")
    ok(r == "OK PA 16000", "HELLO 应答 (%s)" % r)
    files = list_files()
    print("  板上文件:", files)

    print("== 2. 上传(含故意丢块) ==")
    data = make_pcm(3.0)
    ok(upload("测试音频", data, skip_chunks=4), "上传+丢块重传+crc 校验")
    files = list_files()
    ok(any(f.startswith("测试音频|") and f.endswith("|%08x" % (zlib.crc32(data) & 0xFFFFFFFF))
           for f in files), "LIST 能看到新文件且 crc 一致: %s" % files)

    print("== 3. PLAY / STOP ==")
    r = ctrl("PLAY 测试音频")
    ok(r == "OK PLAY 测试音频", "PLAY 应答 (%s)" % r)
    time.sleep(1.2)
    r = ctrl("STOP")
    ok(r == "OK STOP", "STOP 应答 (%s)" % r)
    time.sleep(0.5)

    print("== 4. PLAY 到自然结束 ==")
    ctrl("PLAY 测试音频")
    time.sleep(4.5)                        # 3s 音频 + 余量

    print("== 5. 对讲丢包统计 ==")
    r = talk_burst(150, 160)
    print("  STAT:", r)
    ok(r == "STAT recv=150 sent=160 loss=6", "收150/发160 -> 丢6%% (%s)" % r)

    print("== 6. RATE 切换 ==")
    r = ctrl("RATE 8000")
    ok(r == "OK RATE 8000", "RATE 8000 (%s)" % r)
    print(" ", ctrl("TALK 1"))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for _ in range(25):
        s.sendto(b"\x10\x20" * 320, (HOST, AUDIO))   # 8k 40ms
        time.sleep(0.04)
    s.close()
    print(" ", ctrl("TALK 0 25"))
    r = ctrl("RATE 16000")
    ok(r == "OK RATE 16000", "RATE 切回 16000 (%s)" % r)
    r = ctrl("RATE 123")
    ok(r == "ERR BADRATE", "非法 RATE 被拒 (%s)" % r)

    print("== 7. REN / DEL / 非法名 ==")
    ok(ctrl("REN 测试音频 测试改名") == "OK REN", "REN")
    files = list_files()
    ok(any(f.startswith("测试改名|") for f in files), "改名后可见: %s" % files)
    ok(ctrl("DEL 测试改名") == "OK DEL", "DEL")
    ok(list_files() == [], "DEL 后清空")
    ok(ctrl("PLAY ../etc/passwd") == "ERR BADNAME", "路径穿越被拒")
    ok(ctrl("DEL 不存在啊") == "ERR NOFILE", "DEL 不存在")

    print("== 8. 上传同名覆盖(防脏数据) ==")
    data2 = make_pcm(1.5)
    ok(upload("覆盖测试", data2), "上传 覆盖测试")
    ok(upload("覆盖测试", make_pcm(1.0)), "同名重传")
    ok(ctrl("DEL 覆盖测试") == "OK DEL", "清理")

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), FAILS))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
