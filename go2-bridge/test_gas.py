# -*- coding: utf-8 -*-
"""test_gas.py -- 气体传感器读取模块(gas.py)离线测试。
黄金向量=2026-11-03 dog4 真机抓帧: 010314000000040014002300640001023401023F00035A697F
用法: python run_test_shim.py test_gas.py
"""
import time

FAILS = []


def ok(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


import gas  # noqa: E402

# ---- 1. CRC / 问询帧: 手册样例自检 ----
ok(gas.crc16(bytes([1, 3, 0, 0, 0, 10])) == bytes([0xC5, 0xCD]),
   "crc16 与手册样例一致(01 03 00 00 00 0A -> C5 CD)")
ok(gas.build_query(1) == bytes.fromhex("01030000000AC5CD"),
   "build_query(1) == 01030000000AC5CD")
ok(gas.build_query(13) == bytes.fromhex("0D030000000AC501"),
   "build_query(13) CRC 正确(地址扫描实测向量)")

# ---- 2. 真机黄金帧解析 ----
GOLD = bytes.fromhex("010314000000040014002300640001023401023F00035A697F")
p = gas.parse_frame(GOLD)
ok(p is not None, "真机帧解析成功")
ok(p["conc"] == 4 and p["range"] == 100, "浓度=4ppm 量程=100(NH3 0-100ppm)")
ok(p["status"] == 1 and p["status_txt"] == "正常", "状态=1 正常")
ok(p["low"] == 20 and p["high"] == 35, "低报=20 高报=35")
ok(p["gas"] == 63, "气体类型=0x3F=63(NH3 氨气, 标签 NH3 误读 N2)")
ok(p["temp"] == -24.2, "温度硬编码期望 -24.2(公式独立核过, 防恒真)")
ok(p["hum"] == 85.8, "湿度 raw/10 (858 -> 85.8%%)")

# ---- 3. 坏帧拒绝 ----
ok(gas.parse_frame(GOLD[:-1]) is None, "截断帧拒绝")
ok(gas.parse_frame(GOLD[:-2] + b"\x00\x00") is None, "CRC 错拒绝")
bad_func = bytearray(GOLD)
bad_func[1] = 0x04
ok(gas.parse_frame(bytes(bad_func)) is None, "功能码非 03 拒绝")
ok(gas.parse_frame(None) is None, "None 拒绝")
ok(gas.parse_frame(b"") is None, "空帧拒绝")

# ---- 4. 小数位: 寄存器0 bit11-8=0100(1位小数) 时浓度 /10 ----
f1 = bytearray(GOLD)
f1[3:5] = b"\x01\x00"          # 单位/小数位: 0x0100 -> dec=1
f1[5:7] = b"\x00\x25"          # 浓度 37 -> 3.7
# 重算 CRC
newcrc = gas.crc16(bytes(f1[:23]))
f1[23:25] = newcrc
p1 = gas.parse_frame(bytes(f1))
ok(p1 is not None and p1["dec"] == 1 and abs(p1["conc"] - 3.7) < 1e-9,
   "小数位 dec=1 时浓度 37 -> 3.7")

# ---- 5. _poll_once + 状态迁移回调(假串口) ----
import struct  # noqa: E402  (帧构造用)

calls = []


class FakeSer:
    """最小 serial 桩: 内部字节缓冲, read(n) 最多吐 n 字节(与真串口同语义)。"""

    def __init__(self, data=b""):
        self.buf = bytes(data)
        self.written = []

    def reset_input_buffer(self):
        pass

    def write(self, b):
        self.written.append(b)
        return len(b)

    def read(self, n):
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


calls = []


def frame_with_status(st, addr=1):
    b = bytearray(GOLD)
    b[0] = addr
    b[13:15] = bytes([0, st])            # 寄存器5: 状态字
    b[23:25] = gas.crc16(bytes(b[:23]))
    return bytes(b)


# ---- 5. 单地址轮询 + 状态迁移回调 ----
g = gas.GasBus("/dev/null", addrs=[1], notify=lambda a, o, n, s: calls.append((a, o, n)))
got = g._poll_once(FakeSer(GOLD), 1)
ok(got is not None and g._slots[1]["ok"] is True and g._slots[1]["total"] == 1,
   "_poll_once 好帧 -> 槽1 ok=True total+1")
sn = g.snapshot()
ok(sn["ok"] is True and sn["n_present"] == 1 and len(sn["sensors"]) == 1,
   "snapshot: ok / n_present=1 / 单槽列表")
ok(sn["sensors"][0]["age"] is not None and "ok_t" not in sn["sensors"][0],
   "槽快照暴露 age 不漏 ok_t")
ok(sn["sensors"][0]["gas_name"] == "NH3", "类型码 63 -> NH3 名称")

# 连续坏帧: bad 计数, ok 翻 False
g._poll_once(FakeSer(b"\x00" * 25), 1)
g._poll_once(FakeSer(b""), 1)
ok(g._slots[1]["ok"] is False and g._slots[1]["bad"] == 2, "坏帧 -> ok=False bad=2")
ok(g._slots[1]["err"] == "bad_frame", "坏帧 err=bad_frame")

# ---- 6. 多地址: 2 号槽未接(absent), 1 号槽正常 ----
g2 = gas.GasBus("/dev/null", addrs=[1, 2],
                notify=lambda a, o, n, s: calls.append((a, o, n)))
g2._poll_once(FakeSer(GOLD), 1)
g2._poll_once(FakeSer(b""), 2)            # 2 号无应答
s2 = g2.snapshot()
ok(s2["n_present"] == 1 and s2["ok"] is True, "双槽: 1 号在位 2 号未接")
ok(s2["sensors"][1]["present"] is False and s2["sensors"][1]["err"] == "bad_frame",
   "未接槽 present=False err=bad_frame")

# 状态迁移: 1 号 正常(1) -> 低报(5) 触发一次回调(带地址), 5->5 不触发
g2._poll_once(FakeSer(frame_with_status(1)), 1)
g2._poll_once(FakeSer(frame_with_status(5)), 1)
g2._poll_once(FakeSer(frame_with_status(5)), 1)
ok(calls and calls[-1] == (1, 1, 5), "状态 1->5 触发回调(带 addr)")
ok(len([c for c in calls if c[:3] == (1, 1, 5)]) == 1, "5->5 同值不重复触发")

# ---- 7. 帧头对齐: 前面混入垃圾字节仍能解出 ----
g3 = gas.GasBus("/dev/null", addrs=[1])
junk = b"\xff\x01\x03\x14" + GOLD      # 假同步头+完整帧
got3 = g3._poll_once(FakeSer(junk), 1)
ok(got3 is not None, "前导垃圾字节按帧头对齐后仍解析成功")

# ---- 8. build_query 多地址向量(手册多气体轮询) ----
ok(gas.build_query(2) == bytes.fromhex("02030000000AC5FE"),
   "build_query(2) == 02030000000AC5FE(手册六气体轮询示例)")
ok(gas.build_query(6) == bytes.fromhex("06030000000AC47A"),
   "build_query(6) == 06030000000AC47A(手册示例)")

print()
if FAILS:
    print("失败 %d 项: %s" % (len(FAILS), FAILS))
    raise SystemExit(1)
print("gas 全部通过")
