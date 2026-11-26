# -*- coding: utf-8 -*-
"""SBUS 解码: 100000bps 8E2, 25字节一帧, 16通道各11位(172~1811, 中值992)"""
import serial


class Sbus:
    def __init__(self, port="/dev/ttyUSB0"):
        self.ser = serial.Serial(port=port, baudrate=100000,
                                 bytesize=serial.EIGHTBITS,
                                 parity=serial.PARITY_EVEN,
                                 stopbits=serial.STOPBITS_TWO, timeout=0.02)
        self.buf = bytearray()

    def read(self):
        """解析出一帧: (16通道列表, 失控标志, 丢帧标志); 无完整帧返回 None"""
        self.buf += self.ser.read(64)
        while True:
            i = self.buf.find(b"\x0f")
            if i < 0:
                self.buf = bytearray()
                return None
            if i:
                del self.buf[:i]
                continue
            if len(self.buf) < 25:
                return None
            frame = bytes(self.buf[:25])
            if frame[24] != 0x00:      # 帧尾校验(个别接收机帧尾非0x00时可放宽)
                del self.buf[:1]
                continue
            del self.buf[:25]
            bits = int.from_bytes(frame[1:23], "little")
            ch = [(bits >> (11 * k)) & 0x7FF for k in range(16)]
            flags = frame[23]
            return ch, bool(flags & 0x08), bool(flags & 0x04)
