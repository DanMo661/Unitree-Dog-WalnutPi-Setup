# -*- coding: utf-8 -*-
"""板级声道策略自测(T3.2, 无需板子/声卡):
1. config_local.OVERRIDES 逐键覆盖: 只覆盖已有白名单键, 未定义键忽略
2. PA_STEREO=False 时预设播放与对讲两条路径都走 L-only(右声道置零)
3. PA_STEREO=True 时两条路径都是左右同数据
用法: python run_test_shim.py test_pa_stereo.py   (或 python test_pa_stereo.py, 本文件自带 audioop 垫片)
"""
import array
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types

# ---- audioop 垫片(本地 py3.13+ 无 audioop; run_test_shim 只补了 mul/max) ----
try:
    import audioop
except ImportError:
    audioop = types.ModuleType("audioop")
    sys.modules["audioop"] = audioop

if not hasattr(audioop, "tostereo"):
    def _tostereo(fragment, width, lfactor, rfactor):
        a = array.array("h")
        a.frombytes(fragment)
        out = array.array("h")
        for v in a:
            out.append(int(v * lfactor))
            out.append(int(v * rfactor))
        return out.tobytes()
    audioop.tostereo = _tostereo

import pa_recv

HERE = os.path.dirname(os.path.abspath(__file__))
FAILS = []
MONO = b"\x01\x00\x02\x00"                       # 两个样本: 1, 2
BOTH = b"\x01\x00\x01\x00\x02\x00\x02\x00"       # 左右同数据
LEFT = b"\x01\x00\x00\x00\x02\x00\x00\x00"       # L-only


def ok(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


class FakeProc(object):
    def __init__(self):
        self.buf = bytearray()
        self.stdin = self

    def write(self, b):
        self.buf += b

    def close(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class FakePa(object):
    volume = 100                                  # 跳过软件增益, 只看声道变换


def check_config_override():
    print("== 1. config_local.OVERRIDES 覆盖规则 ==")
    tmp = tempfile.mkdtemp()
    try:
        shutil.copy(os.path.join(HERE, "config.py"), tmp)

        def probe():
            code = ("import config; print(config.PA_STEREO, "
                    "hasattr(config, 'NOT_A_KEY'))")
            r = subprocess.run([sys.executable, "-c", code], cwd=tmp,
                               capture_output=True, text=True)
            return r.stdout.split(), r.returncode

        out, rc = probe()
        ok(rc == 0 and out == ["True", "False"], "无 config_local: 默认 PA_STEREO=True (%s)" % out)

        with open(os.path.join(tmp, "config_local.py"), "w", encoding="utf-8") as f:
            f.write("OVERRIDES = {'PA_STEREO': False, 'NOT_A_KEY': 1}\n")
        out, rc = probe()
        ok(rc == 0 and out == ["False", "False"], "有 config_local: False 生效且未定义键被忽略 (%s)" % out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def play_once(stereo):
    tmpdir = tempfile.mkdtemp()
    old_dir, old_sub, old_cfg = pa_recv.FILES_DIR, pa_recv.subprocess, pa_recv.config
    try:
        with open(os.path.join(tmpdir, "t.pcm"), "wb") as f:
            f.write(MONO)
        pa_recv.FILES_DIR = tmpdir
        proc = FakeProc()
        pa_recv.subprocess = types.SimpleNamespace(PIPE=None, Popen=lambda *a, **k: proc)
        pa_recv.config = types.SimpleNamespace(PA_STEREO=stereo)
        pa_recv._player["stop"] = False
        pa_recv._player_thread("t", FakePa(), lambda *a: None)
        return bytes(proc.buf)
    finally:
        pa_recv.FILES_DIR, pa_recv.subprocess, pa_recv.config = old_dir, old_sub, old_cfg
        shutil.rmtree(tmpdir, ignore_errors=True)


def talk_once(stereo):
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()                                 # 让出端口给 _loop 绑 0.0.0.0
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    proc = FakeProc()
    old_spawn, old_cfg = pa_recv._spawn_aplay, pa_recv.config
    try:
        pa_recv._spawn_aplay = lambda rate, log: proc
        pa_recv.config = types.SimpleNamespace(PA_STEREO=stereo, PA_UDP_PORT=port)
        threading.Thread(target=pa_recv._loop, args=(FakePa(), pa_recv.config, lambda *a: None),
                         daemon=True).start()
        deadline = time.time() + 2.0
        while not proc.buf and time.time() < deadline:
            s.sendto(MONO, ("127.0.0.1", port))
            time.sleep(0.05)
        return bytes(proc.buf)
    finally:
        s.close()
        pa_recv._spawn_aplay, pa_recv.config = old_spawn, old_cfg


def main():
    check_config_override()

    print("== 2. PA_STEREO=False: 两条路径都 L-only ==")
    ok(play_once(False) == LEFT, "预设播放 L-only")
    ok(talk_once(False) == LEFT, "对讲音频 L-only")

    print("== 3. PA_STEREO=True: 两条路径都左右同数据 ==")
    ok(play_once(True) == BOTH, "预设播放双声道")
    ok(talk_once(True) == BOTH, "对讲音频双声道")

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), FAILS))
        sys.exit(1)
    print("test_pa_stereo: 全部通过")


if __name__ == "__main__":
    main()
