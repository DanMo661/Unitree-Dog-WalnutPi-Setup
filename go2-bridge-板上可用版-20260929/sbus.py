# -*- coding: utf-8 -*-
"""SBUS 协议解码
一帧 25 字节: 0x0F + 22 字节数据(16 通道 x 11bit, 低位在前) + 1 字节标志 + 1 字节帧尾
串口 100000bps 8E2; 通道行程 172~1811, 中值 992; 标志字节 bit3=失控 bit2=丢帧
"""
import serial

END_BYTE = 0x00   # 个别接收机帧尾非 0x00 时改这里


def extract_frames(buf: bytearray):
    """从缓冲解出所有完整帧, 返回 (帧列表, 剩余字节)。纯函数, 供单元测试。"""
    frames = []
    while True:
        i = buf.find(b"\x0f")
        if i < 0:
            buf.clear()
            break
        if i:
            del buf[:i]
            continue
        if len(buf) < 25:
            break
        if buf[24] != END_BYTE:      # 假同步, 丢 1 字节继续扫
            del buf[:1]
            continue
        if len(buf) > 25 and buf[25] != 0x0F:
            # 帧尾后必须紧跟下帧帧头(SBUS 背靠背), 否则是标志/数据区撞出来的假同步
            del buf[:1]
            continue
        frames.append(bytes(buf[:25]))
        del buf[:25]
    return frames, buf


def decode_frame(frame: bytes):
    """25 字节帧 -> (16 通道列表, 失控标志, 丢帧标志)"""
    bits = int.from_bytes(frame[1:23], "little")
    ch = [(bits >> (11 * k)) & 0x7FF for k in range(16)]
    flags = frame[23]
    return ch, bool(flags & 0x08), bool(flags & 0x04)


def pack_frame(ch, failsafe=False, lost=False):
    """打包一帧(单元测试/模拟输入用), ch 为 16 个 0~2047 的整数"""
    bits = 0
    for k, v in enumerate(ch):
        bits |= (v & 0x7FF) << (11 * k)
    flags = (0x08 if failsafe else 0) | (0x04 if lost else 0)
    return b"\x0f" + bits.to_bytes(22, "little") + bytes([flags, END_BYTE])


class SbusReader:
    """流式组帧器: feed() 喂任意长度原始字节, 返回新解出的完整帧列表"""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf += data
        frames, self.buf = extract_frames(self.buf)
        return frames


class Sbus:
    """串口读取器: read() 返回最新一帧 (ch, 失控, 丢帧) 或 None; 打不开/拔出可反复 open()"""

    def __init__(self, port, on_frames=None):
        self.port = port
        self.ser = None
        self.reader = SbusReader()
        self.on_frames = on_frames   # 可选: 每批解出的帧列表回调(调试统计用), 异常被吞不影响控制

    @property
    def opened(self):
        return self.ser is not None and self.ser.is_open

    def open(self):
        try:
            self.ser = serial.Serial(port=self.port, baudrate=100000,
                                     bytesize=serial.EIGHTBITS,
                                     parity=serial.PARITY_EVEN,
                                     stopbits=serial.STOPBITS_TWO, timeout=0.02)
            return True
        except Exception:
            self.ser = None
            return False

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def read(self):
        """返回最新一帧; 无数据/串口不可用返回 None(调用方负责 open 重试)"""
        if not self.opened:
            return None
        try:
            data = self.ser.read(256)
        except Exception:
            self.close()
            return None
        batch = [decode_frame(f) for f in self.reader.feed(data)]
        if self.on_frames and batch:
            try:
                self.on_frames(batch)
            except Exception:
                pass
        return batch[-1] if batch else None
