# -*- coding: utf-8 -*-
"""QZKJ 气体传感器模组(UART TTL, Modbus-RTU 9600 8N1 从机地址出厂 0x01)读取线程。

2026-11-03 dog4 加装(前兆 QZKJ 系列), 11-04 起多传感器总线:
  同一条 UART 挂最多 6 颗模组, 各占一个 Modbus 地址(手册"多气体轮询"拓扑, 地址 1..6),
  逐地址问询(无应答=该槽未接, 短超时快扫), 快照按地址列表给前端。
实测帧协议(手册+真机抓帧双确认):
  查询: <addr> 03 00 00 00 0A + CRC16(低前)          例: 01 03 00 00 00 0A C5 CD
  应答: <addr> 03 14 <10x16bit 大端寄存器> + CRC16(低前)   共 25 字节
寄存器: 0 单位/小数位  1 浓度  2 低报  3 高报  4 量程  5 状态  6 AD
        7 温度(raw, 实际℃=(raw-500)/10)  8 高8位=气体类型  9 湿度(raw/10 %)

定位: 非安全件 —— 只读, 不碰任何控制路径; 串口打不开/无应答/帧坏 -> snapshot(ok=False),
桥照常跑。接线铁律: 模块 R(收)接对端 TX, 模块 T(发)接对端 RX(11-03 现场踩坑 2 小时)。
dog4 实况: 板上 UART 在 /dev/ttyS3(DTB 已启用 uart3, PI9/PI10), 板级经 config_local 指 GAS_PORT。
"""
import struct
import threading
import time

import serial

REG_N = 10          # 问询寄存器个数
FRAME_LEN = 3 + 2 * REG_N + 2   # 应答帧长: addr+func+bytecount + 数据 + CRC = 25
RESP_BYTES = 0x14   # 应答 bytecount 字段(=20)

STATUS_TXT = {0x00: "预热", 0x01: "正常", 0x02: "数据错误", 0x03: "传感器故障",
              0x04: "预警", 0x05: "低报", 0x06: "高报", 0x07: "访问故障",
              0x08: "超量程", 0x09: "需标定", 0x0A: "超时", 0x0B: "stel报警",
              0x0C: "twa报警", 0x0F: "通信故障"}
# 值得 journal/UI 记一笔的状态(其余状态变化只静默更新快照)
_NOTABLE = frozenset({0x02, 0x03, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0F})

# 气体类型码 -> 名称(手册"气体类型表"摘要; 未列出的显示 "类型<N>")
GAS_NAMES = {5: "CO", 6: "CO2", 7: "COCl2", 11: "CH4", 17: "CL2", 21: "C2H2",
             35: "C6H6", 45: "EX可燃", 46: "ETO", 50: "H2", 52: "H2S",
             53: "HCL", 54: "HCN", 55: "HBR", 57: "HF", 59: "NO", 60: "NO2", 63: "NH3",
             64: "N2", 67: "O2", 68: "O3", 69: "PH3", 72: "SO2", 76: "SF6",
             78: "TVOC", 79: "VOC", 80: "VOCS"}


def gas_name(code):
    return GAS_NAMES.get(code, "类型%d" % code)


def crc16(data):
    """Modbus RTU CRC16(多项式 0xA001), 返回 2 字节(低前高后)。"""
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return bytes([c & 0xFF, (c >> 8) & 0xFF])


def build_query(addr=1):
    """功能码 0x03 读 10 个寄存器(0x0000 起)的完整问询帧。手册样例自检:
    build_query(1) == 01 03 00 00 00 0A C5 CD"""
    body = bytes([addr, 0x03, 0x00, 0x00, 0x00, REG_N])
    return body + crc16(body)


def parse_frame(r):
    """应答帧 -> 快照 dict; 长度/功能码/CRC 任一不对返回 None。纯函数, 供单测。"""
    if r is None or len(r) != FRAME_LEN or r[1] != 0x03 or r[2] != RESP_BYTES:
        return None
    if crc16(r[:FRAME_LEN - 2]) != r[FRAME_LEN - 2:]:
        return None
    regs = [struct.unpack(">H", r[3 + 2 * k:5 + 2 * k])[0] for k in range(REG_N)]
    dec = (regs[0] >> 8) & 0xF               # 小数点位数: bit11-8(0/4/8/12=0~3 位)
    conc = regs[1] / (10 ** dec) if dec < 4 else float(regs[1])
    st = regs[5] & 0xFF
    g = regs[8] >> 8
    return {"unit": regs[0] >> 12, "dec": dec, "conc": conc,
            "low": regs[2], "high": regs[3], "range": regs[4],
            "status": st, "status_txt": STATUS_TXT.get(st, "0x%02X" % st),
            "ad": regs[6], "temp": round((regs[7] - 500) / 10.0, 1),
            "gas": g, "gas_name": gas_name(g), "hum": regs[9] / 10.0}


class GasBus:
    """多传感器读取线程: 逐地址问询, 每地址一份快照(单赋值换引用, GIL 下读侧免锁)。
    notify(addr, status_old, status_new, snap): notable 状态迁移回调(bridge 注入记账);
    只对 _NOTABLE 里的状态迁移触发, 同值不触发。
    未接的地址: 读超时判空, 该槽记 absent(present=False), 不重试不拖慢整轮。"""

    def __init__(self, port, addrs=(1, 2, 3, 4, 5, 6), period=1.5,
                 absent_t=0.3, gap=0.5, log=print, notify=None):
        self.port = port
        self.addrs = [int(a) for a in addrs] or [1]
        self.period = max(0.5, float(period))    # 整轮之间的最小间隔
        self.absent_t = float(absent_t)          # 无应答判定读超时(短扫, 未接快跳)
        self.gap = float(gap)                    # 地址间间隔(手册建议 500ms 以上)
        self._log = log
        self._notify = notify
        self._slots = {a: {"addr": a, "present": False, "ok": False, "err": "starting",
                           "ok_t": None, "status": None, "total": 0, "bad": 0}
                       for a in self.addrs}
        self._stop = False
        self._th = None

    # ---- 线程外接口 ----

    def start(self):
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def snapshot(self, now=None):
        """/state 与 /gas 的只读快照: sensors 按地址序, 附 age(距该槽最近有效帧)。"""
        now = time.monotonic() if now is None else now
        sensors = []
        for a in self.addrs:
            s = dict(self._slots[a])
            if s.get("ok_t") is not None:
                s["age"] = round(now - s.pop("ok_t"), 2)
            else:
                s.pop("ok_t", None)
                s["age"] = None
            sensors.append(s)
        return {"enabled": True,
                "ok": any(x["ok"] for x in sensors),
                "n_present": sum(1 for x in sensors if x.get("present")),
                "sensors": sensors,
                "port": self.port}

    # ---- 内部 ----

    def _publish(self, addr, parsed=None, err=None):
        """换该槽快照引用 + notable 状态迁移回调。parsed=None 表示本轮无有效帧。"""
        s = dict(self._slots[addr])
        s["addr"] = addr
        s["err"] = err
        old_st = s.get("status")                 # 旧状态必须先取, update 会覆盖
        if parsed is not None:
            s.update(parsed)
            s["present"] = True
            s["ok"] = True
            s["ok_t"] = time.monotonic()
            s["total"] = s.get("total", 0) + 1
            new_st = parsed["status"]
            if (new_st != old_st and self._notify is not None
                    and (new_st in _NOTABLE or old_st in _NOTABLE)):
                try:
                    self._notify(addr, old_st, new_st, parsed)
                except Exception:
                    pass
        else:
            s["ok"] = False
            s["bad"] = s.get("bad", 0) + 1
        self._slots[addr] = s

    def _poll_once(self, s, addr):
        """对单地址一次问询-应答(线程与单测复用): s 为已打开的 serial 实例。
        应答前混入垃圾字节(调试残留/回声)时按帧头 <addr 03 14> 有界重同步,
        假同步从下一字节重扫, 最多 8 轮, 防垃圾流无限吃 CPU。"""
        q = build_query(addr)
        s.reset_input_buffer()
        s.write(q)
        r = s.read(FRAME_LEN)
        hdr = bytes([addr, 0x03, RESP_BYTES])
        parsed = parse_frame(r)
        tried = 0
        while parsed is None and tried < 8:
            tried += 1
            i = r.find(hdr)
            if i < 0:
                more = s.read(FRAME_LEN)          # 没见到帧头就再等一帧量
                if not more:
                    break                          # 对端真没说 anything
                r = (r + more)[-FRAME_LEN:]        # 只留尾部窗口, 防垃圾无限涨
                continue
            cand = r[i:]
            if len(cand) < FRAME_LEN:
                cand += s.read(FRAME_LEN - len(cand))
            parsed = parse_frame(cand)
            if parsed is not None:
                break
            r = cand[1:]                           # 假同步: 丢这个帧头从下一字节重扫
        self._publish(addr, parsed, None if parsed else "bad_frame")
        return parsed

    def _loop(self):
        s = None
        while not self._stop:
            try:
                if s is None or not getattr(s, "is_open", True):
                    s = serial.Serial(self.port, 9600, bytesize=8, parity="N",
                                      stopbits=1, timeout=self.absent_t)
                    self._log("气体传感器串口已打开:", self.port,
                              "地址", ",".join(str(a) for a in self.addrs))
                t0 = time.monotonic()
                for a in self.addrs:
                    if self._stop:
                        break
                    self._poll_once(s, a)
                    time.sleep(self.gap)
            except Exception as e:
                try:
                    if s is not None:
                        s.close()
                except Exception:
                    pass
                s = None
                for a in self.addrs:               # 串口级故障: 全槽标 err
                    self._publish(a, None, repr(e))
                time.sleep(3.0)
                continue
            rest = self.period - (time.monotonic() - t0)
            time.sleep(max(0.2, rest))
        try:
            if s is not None:
                s.close()
        except Exception:
            pass
