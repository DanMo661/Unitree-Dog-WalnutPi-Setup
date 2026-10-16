# -*- coding: utf-8 -*-
"""SBUS 解码单元测试(无需硬件): .venv/bin/python test_sbus.py"""
import random

from sbus import SbusReader, decode_frame, pack_frame


def test_roundtrip():
    """随机通道值 x 失控/丢帧标志组合, 打包后能无损解出"""
    random.seed(1)
    ch = [random.randint(172, 1811) for _ in range(16)]
    for fs, lost in ((False, False), (True, False), (False, True), (True, True)):
        r = SbusReader()
        frames = r.feed(b"\x00\x11\x0f" + pack_frame(ch, fs, lost) + pack_frame(ch))
        assert len(frames) == 2, frames
        got, gfs, glost = decode_frame(frames[0])
        assert got == ch and gfs == fs and glost == lost


def test_resync():
    """前导垃圾字节(含假 0x0F)不破坏同步"""
    r = SbusReader()
    junk = b"\x0f\x0f" + bytes(range(10))
    frames = r.feed(junk + pack_frame([992] * 16))
    assert len(frames) == 1, frames
    ch, fs, lost = decode_frame(frames[0])
    assert ch == [992] * 16 and not fs and not lost


def test_split():
    """一帧从任意位置拆成两半喂入, 也能组出"""
    r = SbusReader()
    f = pack_frame([172, 1811] + [992] * 14, lost=True)
    assert r.feed(f[:7]) == []
    frames = r.feed(f[7:])
    assert len(frames) == 1
    ch, _, lost = decode_frame(frames[0])
    assert ch == [172, 1811] + [992] * 14 and lost


def test_extremes():
    """边界值 0/2047/172/1811 不丢位"""
    ch = [0, 2047, 172, 1811] + [992] * 12
    ch2, _, _ = decode_frame(pack_frame(ch))
    assert ch2 == ch


def test_false_sync_inside():
    """数据区里恰好出现 0x0F 时, 靠帧尾校验跳过假同步"""
    r = SbusReader()
    f = pack_frame([0x0F] * 16)
    frames = r.feed(b"\x0f\x0f\x0f" + f + f)
    assert len(frames) == 2, frames
    assert all(decode_frame(x)[0] == [0x0F] * 16 for x in frames)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(name, "OK")
    print("SBUS 解码全部通过")
