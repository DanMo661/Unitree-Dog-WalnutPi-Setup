# -*- coding: utf-8 -*-
"""KGG(酷狗新版加密音频)离线解锁 -> 通用音频(mp3/flac)
PC 端工具, 供预设音频库铺歌: 解锁后 ffmpeg 转 16k 单声道 pcm 再传板子。

算法忠实移植自 Kugo-Music-Converter backend/internal/algo/kgg(unlock-music 同源):
  1. KGMusicV3.db 逐页 AES-128-CBC 解密(主密钥=社区公开常量) -> 内存 SQLite
  2. kgg 头部 audioHash -> ShareFileItems 表查 ekey
  3. ekey -> TEA 解出 QMC2 原始密钥 -> 按 key 长度选 MAP(<=300)/RC4(>300) 流解密音频体

用法: python kgg_unlock.py <in.kgg> <out音频> [KGMusicV3.db路径]
"""
import base64
import hashlib
import math
import os
import sqlite3
import struct
import sys

M32 = 0xFFFFFFFF
PAGE = 0x400
SQLITE_HDR = b"SQLite format 3\x00"
KGM_MAGIC = bytes([0x7C, 0xD5, 0x32, 0xEB, 0x86, 0x02, 0x7F, 0x4B,
                   0xA8, 0xAF, 0xA6, 0x8E, 0x0F, 0xFF, 0x99, 0x14])
MASTER = bytes([0x1D, 0x61, 0x31, 0x45, 0xB2, 0x47, 0xBF, 0x7F,
                0x3D, 0x18, 0x96, 0x72, 0x14, 0x4F, 0xE4, 0xBF])
V2_PREFIX = b"QQMusic EncV2,Key:"
V2_KEY1 = bytes([0x33, 0x38, 0x36, 0x5A, 0x4A, 0x59, 0x21, 0x40,
                 0x23, 0x2A, 0x24, 0x25, 0x5E, 0x26, 0x29, 0x28])
V2_KEY2 = bytes([0x2A, 0x2A, 0x23, 0x21, 0x28, 0x23, 0x24, 0x25,
                 0x26, 0x5E, 0x61, 0x31, 0x63, 0x5A, 0x2C, 0x54])
DELTA = 0x9E3779B9


# ---------- KGMusicV3.db 逐页解密 ----------

def _aes_cbc(data, key, iv):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()


def _next_iv(seed):
    left = (seed * 0x9EF4) & M32
    right = ((seed // 0xCE26) * 0x7FFFFF07) & M32
    v = (left - right) & M32
    return v if not v & 0x80000000 else (v + 0x7FFFFF07) & M32


def _page_key(no):
    return hashlib.md5(MASTER + struct.pack('<II', no, 0x546C4173)).digest()


def _page_iv(no):
    seed, raw = (no + 1) & M32, bytearray(16)
    for i in range(0, 16, 4):
        seed = _next_iv(seed)
        struct.pack_into('<I', raw, i, seed)
    return hashlib.md5(bytes(raw)).digest()


def decrypt_db(buf):
    if buf[:16] == SQLITE_HDR:
        return buf                                  # 本来就没加密
    if not buf or len(buf) % PAGE:
        raise ValueError("db 大小不对: %d" % len(buf))
    # 页1: 前 0x10 是自定义头, 密文区从 0x10 起; 用明文区镜像做完整性校验
    page = bytearray(buf[:PAGE])
    o10 = struct.unpack_from('<I', page, 0x10)[0]
    o14 = struct.unpack_from('<I', page, 0x14)[0]
    v6 = ((o10 & 0xFF) << 8) | ((o10 & 0xFF00) << 16)
    if o14 != 0x20204000 or (v6 - 0x200) & M32 > 0xFE00 or ((v6 - 1) & v6):
        raise ValueError("db 页1明文头校验不过(不是酷狗加密库?)")
    expected = bytes(page[0x10:0x18])
    page[0x10:0x18] = page[0x08:0x10]
    page[0x10:] = _aes_cbc(bytes(page[0x10:]), _page_key(1), _page_iv(1))
    if bytes(page[0x10:0x18]) != expected:
        raise ValueError("db 页1完整性校验失败(master key 不匹配)")
    out = bytearray(buf)
    out[0:PAGE] = page
    out[0:16] = SQLITE_HDR
    for no in range(2, len(buf) // PAGE + 1):
        off = (no - 1) * PAGE
        out[off:off + PAGE] = _aes_cbc(bytes(out[off:off + PAGE]),
                                       _page_key(no), _page_iv(no))
    return bytes(out)


def load_keymap(db_path):
    plain = decrypt_db(open(db_path, 'rb').read())
    con = sqlite3.connect(':memory:')
    con.deserialize(plain)
    m = {}
    for kid, ek in con.execute(
            "SELECT EncryptionKeyId, EncryptionKey FROM ShareFileItems"):
        if kid and ek:
            m[kid] = ek
    con.close()
    return m


# ---------- ekey -> QMC2 原始密钥 (unlock-music key_derive) ----------

def _tea_dec_block(b, key):
    # x/crypto/tea NewCipherWithRounds(key, 32): rounds/2=16 对迭代, sum 初值 delta*16
    v0, v1 = struct.unpack_from('>II', b, 0)
    k0, k1, k2, k3 = struct.unpack('>IIII', key)
    s = (DELTA * 16) & M32
    for _ in range(16):
        v1 = (v1 - ((((v0 << 4) & M32) + k2) ^ ((v0 + s) & M32)
                    ^ (((v0 >> 5) & M32) + k3))) & M32
        v0 = (v0 - ((((v1 << 4) & M32) + k0) ^ ((v1 + s) & M32)
                    ^ (((v1 >> 5) & M32) + k1))) & M32
        s = (s - DELTA) & M32
    return struct.pack('>II', v0, v1)


def _tea_cbc_decrypt(inbuf, key):
    """Tencent TEA-CBC(salt 2B + pad + zero 7B), 与 x/crypto/tea 32轮逐行对齐"""
    if len(inbuf) % 8 or len(inbuf) < 16:
        raise ValueError("ekey 长度不是 8 的倍数或太短")
    dest = bytearray(_tea_dec_block(inbuf[:8], key))
    pad_len = dest[0] & 0x7
    out_len = len(inbuf) - 1 - pad_len - 2 - 7
    out = bytearray(out_len)
    iv_prev = bytearray(8)
    iv_cur = inbuf[:8]
    pos, di = 8, 1 + pad_len

    def crypt_block():
        nonlocal iv_prev, iv_cur, pos, di
        iv_prev, iv_cur = iv_cur, inbuf[pos:pos + 8]
        for i in range(8):
            dest[i] ^= iv_cur[i]
        dest[:] = _tea_dec_block(dest, key)
        pos += 8
        di = 0

    i = 1
    while i <= 2:                       # 跳过 2 字节 salt
        if di < 8:
            di += 1
            i += 1
        else:
            crypt_block()
    op = 0
    while op < out_len:                 # 明文主体
        if di < 8:
            out[op] = dest[di] ^ iv_prev[di]
            di += 1
            op += 1
        else:
            crypt_block()
    if di < 8 and dest[di] != iv_prev[di]:
        raise ValueError("ekey TEA zero 校验失败(密钥错?)")
    return bytes(out)


def _simple_make_key(salt, n):
    out = bytearray(n)
    for i in range(n):
        out[i] = int(abs(math.tan(salt + i * 0.1)) * 100.0) & 0xFF
    return bytes(out)


def _derive_v1(raw):
    if len(raw) < 16:
        raise ValueError("ekey 太短")
    sk = _simple_make_key(106, 8)
    tk = bytearray(16)
    for i in range(8):
        tk[i * 2] = sk[i]
        tk[i * 2 + 1] = raw[i]
    return raw[:8] + _tea_cbc_decrypt(raw[8:], bytes(tk))


def decrypt_ekey(ekey):
    raw = base64.b64decode(ekey)
    if raw.startswith(V2_PREFIX):
        raw = _tea_cbc_decrypt(_tea_cbc_decrypt(raw[len(V2_PREFIX):], V2_KEY1), V2_KEY2)
        raw = base64.b64decode(raw)
    return _derive_v1(raw)


# ---------- QMC2 流解密 (MAP / RC4) ----------

class QMC2Map:
    def __init__(self, key):
        self.n = len(key)
        self.masks = bytes(self._mask(i, key, self.n) for i in range(0x8000))

    @staticmethod
    def _mask(off, key, n):
        if off > 0x7FFF:
            off %= 0x7FFF
        idx = (off * off + 71214) % n
        v = key[idx]
        r = ((idx & 0x7) + 4) % 8
        return ((v << r) | (v >> r)) & 0xFF     # 上游刻意非循环移位, 勿改

    def decrypt(self, buf, offset):
        period = 0x7FFF
        pos = 0
        while pos < len(buf):
            if offset <= period:
                mo, chunk = offset, period - offset + 1
            else:
                mo, chunk = offset % period, period - offset % period
            chunk = min(chunk, len(buf) - pos)
            for i in range(chunk):
                buf[pos + i] ^= self.masks[mo + i]
            pos += chunk
            offset += chunk


class QMC2RC4:
    SEG, FIRST = 5120, 128

    def __init__(self, key):
        self.key, self.n = key, len(key)
        # Go 版 box 是 []byte: byte(i) 截断, 密钥>256字节时盒值只有 0..255
        box = [i & 0xFF for i in range(self.n)]
        j = 0
        for i in range(self.n):
            j = (j + box[i] + key[i % self.n]) % self.n
            box[i], box[j] = box[j], box[i]
        self.box = box
        h = 1
        for i in range(self.n):
            v = key[i]
            if not v:
                continue
            nh = (h * v) & M32
            if nh == 0 or nh <= h:
                break
            h = nh
        self.hash = h

    def _seg_skip(self, sid):
        seed = self.key[sid % self.n]
        idx = int(self.hash / ((sid + 1) * seed) * 100.0)
        return idx % self.n

    def _first(self, b, off):
        for i in range(len(b)):
            b[i] ^= self.key[self._seg_skip(off + i)]

    def _aseg(self, b, off):
        box = self.box[:]
        j = k = 0
        skip = off % self.SEG + self._seg_skip(off // self.SEG)
        for i in range(-skip, len(b)):
            j = (j + 1) % self.n
            k = (box[j] + k) % self.n
            box[j], box[k] = box[k], box[j]
            if i >= 0:
                b[i] ^= box[(box[j] + box[k]) % self.n]

    def decrypt(self, buf, offset):
        # memoryview 切片共享内存(Go slice 语义); bytearray 切片是拷贝会白干
        buf = memoryview(buf)
        off, togo, done = offset, len(buf), 0

        def mark(p):
            nonlocal off, togo, done
            off += p
            togo -= p
            done += p

        if off < self.FIRST:
            b = min(togo, self.FIRST - off)
            self._first(buf[done:done + b], off)
            mark(b)
            if not togo:
                return
        if off % self.SEG:
            b = min(togo, self.SEG - off % self.SEG)
            self._aseg(buf[done:done + b], off)
            mark(b)
            if not togo:
                return
        while togo > self.SEG:
            self._aseg(buf[done:done + self.SEG], off)
            mark(self.SEG)
        if togo:
            self._aseg(buf[done:], off)


def sniff(buf):
    buf = bytes(buf[:4])
    if buf[:3] == b'ID3' or (len(buf) > 1 and buf[0] == 0xFF and buf[1] & 0xE0 == 0xE0):
        return 'mp3'
    return {b'fLaC': 'flac', b'OggS': 'ogg', b'RIFF': 'wav'}.get(buf, '')


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    inp, outp = sys.argv[1], sys.argv[2]
    db = (sys.argv[3] if len(sys.argv) > 3 else
          os.path.join(os.environ['APPDATA'], 'KuGou8', 'KGMusicV3.db'))
    with open(inp, 'rb') as f:
        head = f.read(0x44)
        if head[:16] != KGM_MAGIC:
            raise SystemExit("不是 kgm/kgg 加密文件(magic 不符)")
        audio_off, ver = struct.unpack_from('<II', head, 0x10)
        if ver != 5:
            raise SystemExit("不支持的加密版本 %d" % ver)
        hlen = struct.unpack('<I', f.read(4))[0]
        ahash = f.read(hlen).decode('utf-8', 'replace')
        f.seek(audio_off)
        audio = bytearray(f.read())

    keymap = load_keymap(db)
    ekey = (keymap.get(ahash) or keymap.get(ahash.upper())
            or keymap.get(ahash.lower()))
    if not ekey:
        raise SystemExit("库中找不到这首歌的 ekey(hash=%s, 库共%d条)。"
                         "先在酷狗里播放一次再试" % (ahash, len(keymap)))
    key = decrypt_ekey(ekey)
    print("ekey 解出: %d 字节原始密钥 -> %s 模式"
          % (len(key), 'RC4' if len(key) > 300 else 'MAP'))
    (QMC2RC4(key) if len(key) > 300 else QMC2Map(key)).decrypt(audio, 0)
    fmt = sniff(audio)
    if not fmt:
        raise SystemExit("解密结果不是合法音频(密钥不对?), 已不落盘")
    if not outp.lower().endswith("." + fmt):
        outp += "." + fmt
    with open(outp, 'wb') as f:
        f.write(audio)
    print("OK %s -> %s (%.1f MB)" % (fmt, outp, len(audio) / 1048576.0))


if __name__ == '__main__':
    sys.exit(main())
