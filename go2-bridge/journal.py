"""journal.py — 飞行记录仪（flight recorder）读写底座。

用途：go2-bridge 运行期黑匣子。主循环各线程（SBUS 解码、网络遥控、状态机、
看门狗）随时 record() 关键事件，出事后按时间轴回放定位；绝不因记录失败
影响主流程（record 全路径静默吞异常）。

格式：JSONL，UTF-8，每行一个独立 JSON 对象、以 \n 结尾。固定字段：
  - t:    time.monotonic() 秒。进程内单调时基，板子无 RTC / wall 时间不可信
          时靠它排序（与 journalctl -o short-monotonic 同口径，见 CLAUDE.md）。
  - wall: 本地时间 ISO8601（datetime.now().isoformat()），仅供人读。
  - kind: 事件类型，调用方给定（如 "sbus" / "fsm" / "wdt" / "netrc"）。
  - 其余字段由 record(kind, **payload) 原样并入；不可 JSON 序列化的值降级为 str。

文件：目录下按 journal-0001.jsonl、journal-0002.jsonl … 顺序编号，单文件超过
max_mb 切下一序号；init 时扫描已有文件续编并续写未满的最后一个文件（重启不覆盖）。

接口：
  init(log_dir=None, max_mb=32) -> bool   目录建不出来/不可写返回 False，不抛异常
  record(kind, **payload)                 追加一行；未 init 或写失败静默返回
  close(clean=True)                       可选，关当前文件；clean=False 保留停机现场
  last_stop()                             最近一次 init 判定的上次停机方式（见下）
  tail(n)                                 只读返回当前文件最后 n 行（n 钳制 1..500）
线程安全：内部 threading.Lock 串行化。

落盘加固（10-20 起立断电事故：断电前约 2 秒的关键记录还在页缓存里全丢）：
  - kind=event、以及 kind=cmd 且 name≠Move 的关键记录：flush 后额外 os.fsync
    强制出页缓存，断电也不丢；
  - Move 流（20Hz）与高频 frame 只 flush 不 fsync，避免每帧刷 TF 卡减寿；
  - fsync 失败静默，绝不上抛，也不影响 _bytes 记账。

停机标记（日志目录内 .running / .clean 两个小文件）：
  - init 时判定上次停机：有 .clean → 'clean'；有 .running 无 .clean → 'unclean'；
    都没有 → 'unknown'（首次运行）。判定值存 last_stop()；随后删 .clean、
    写 .running（内容含 pid 与时间）；
  - close(clean=True) 写 .clean 并删 .running；close(clean=False) 保留 .running，
    下次 init 即判 'unclean'（掉电正是走不到 close 的这种情况）。
"""

import glob
import json
import os
import platform
import re
import threading
import time
from datetime import datetime

_BOARD_DEFAULT = "/home/pi/go2-bridge/journals"  # 板端部署目录下的 journals/
_FALLBACK_DIR = "./journals"
_NAME_RE = re.compile(r"^journal-(\d{4,})\.jsonl$")

_lock = threading.RLock()  # 可重入: SIGTERM 可能在主线程持锁时进来关文件
_fh = None          # 当前打开的日志文件对象；None = 未 init / 已关
_dir = None         # 实际使用的目录
_seq = 0            # 当前文件序号
_bytes = 0          # 当前文件已写字节（含 init 续接的旧内容）
_max_bytes = 32 * 1024 * 1024
_last_stop = None    # 最近一次 init 判定的上次停机方式：clean/unclean/unknown


def _candidates(explicit):
    """init 的目录候选，按优先级；前者失败才试后者。"""
    if explicit:
        return [explicit]  # 显式指定：失败即失败，不偷偷换地方
    if platform.system() == "Linux":
        return [_BOARD_DEFAULT, _FALLBACK_DIR]
    return [_FALLBACK_DIR]  # 开发机（Windows/mac）直接退回本地


def _open(seq):
    """打开（新建/续写）指定序号文件。调用方持锁。"""
    global _fh, _seq, _bytes
    _fh = open(os.path.join(_dir, "journal-%04d.jsonl" % seq),
               "a", encoding="utf-8", newline="")
    _seq = seq
    _bytes = os.fstat(_fh.fileno()).st_size  # 续接旧文件时计入已有内容


def _marker_path(name):
    """标记文件（.running/.clean）全路径；未 init（_dir 为空）返回 None。"""
    return os.path.join(_dir, name) if _dir else None


def _judge_last_stop():
    """按 .clean/.running 存在性判定上次停机方式，存入 _last_stop。调用方持锁。"""
    global _last_stop
    clean = _marker_path(".clean")
    running = _marker_path(".running")
    if clean and os.path.exists(clean):
        _last_stop = "clean"        # .clean 优先：正常走到 close(clean=True)
    elif running and os.path.exists(running):
        _last_stop = "unclean"      # 运行标记还在、却没留下 clean → 掉电/被杀
    else:
        _last_stop = "unknown"      # 首次运行 / 标记被清


def _write_marker(name):
    """写 .running/.clean 标记（内容含 pid 与时间）。失败静默。调用方持锁。"""
    p = _marker_path(name)
    if not p:
        return
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("pid=%d\ntime=%s\n" % (os.getpid(), datetime.now().isoformat()))
    except Exception:
        pass


def _remove_marker(name):
    """删标记文件（不存在则无操作）。失败静默。调用方持锁。"""
    p = _marker_path(name)
    if not p:
        return
    try:
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def init(log_dir=None, max_mb=32):
    """初始化记录仪。成功 True；目录不可用 False。可重复调用（隐式 close 旧文件）。"""
    global _dir, _fh, _max_bytes
    with _lock:
        if _fh is not None:
            try:
                _fh.close()
            except Exception:
                pass
            _fh = None
        for cand in _candidates(log_dir):
            try:
                os.makedirs(cand, exist_ok=True)
                probe = os.path.join(cand, ".probe")
                with open(probe, "w", encoding="utf-8") as f:
                    f.write("")
                os.remove(probe)
            except Exception:
                continue  # 建不了 / 不可写 → 换下一个候选
            _dir = cand
            _max_bytes = max(int(max_mb), 1) * 1024 * 1024
            _judge_last_stop()        # 先判定上次停机方式（结果进 last_stop()）
            _remove_marker(".clean")  # 判定完成即清，防下次误判
            last = 0
            for p in glob.glob(os.path.join(_dir, "journal-*.jsonl")):
                m = _NAME_RE.match(os.path.basename(p))
                if m:
                    last = max(last, int(m.group(1)))
            try:
                _open(max(last, 1))  # 续写最后一个文件（不满则继续填）
            except Exception:
                _fh = None
                return False
            _write_marker(".running")  # 本轮运行中标记（pid+时间）
            return True
        return False


def _rotate():
    """切下一个序号文件。失败则关闭记录（磁盘满等，静默停写）。调用方持锁。"""
    global _fh
    try:
        _fh.close()
        _open(_seq + 1)
    except Exception:
        _fh = None


def record(kind, **payload):
    """追加一条事件。未 init、序列化失败、写失败均静默，绝不上抛。"""
    global _bytes
    with _lock:
        if _fh is None:
            return
        try:
            entry = {"t": time.monotonic(),
                     "wall": datetime.now().isoformat(),
                     "kind": kind}
            entry.update(payload)
            text = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
            nbytes = len(text.encode("utf-8"))
            if _bytes + nbytes > _max_bytes:
                _rotate()
                if _fh is None:
                    return
            _fh.write(text)
            _fh.flush()
            if kind == "event" or (kind == "cmd" and payload.get("name") != "Move"):
                try:
                    os.fsync(_fh.fileno())  # 关键记录强制出页缓存（防断电丢最后几秒）
                except Exception:
                    pass  # fsync 失败静默、绝不上抛；也别让它跳过下面的 _bytes 记账
            _bytes += nbytes
        except Exception:
            pass


def close(clean=True):
    """关闭当前文件。可再次 init 重开。
    clean=True（缺省，正常停机）：写 .clean 并删 .running，下次 init 判 'clean'；
    clean=False（异常退出路径）：保留 .running，下次 init 判 'unclean'。"""
    global _fh
    with _lock:
        if _fh is not None:
            try:
                _fh.close()
            except Exception:
                pass
            _fh = None
        if clean:
            _write_marker(".clean")
            _remove_marker(".running")
        # clean=False：.running 原样保留 → 下次 init 判 'unclean'


def last_stop():
    """最近一次 init 判定的上次停机方式：'clean'/'unclean'/'unknown'；未 init 过为 None。"""
    return _last_stop


def tail(n):
    """只读返回当前文件最后 n 行（不含换行符）；n 钳制 1..500。
    未 init / 当前文件读失败返回 []。持锁读，避免撞上并发写出的半行。"""
    try:
        n = int(n)
    except Exception:
        n = 1
    n = max(1, min(n, 500))
    with _lock:
        if _fh is None or _dir is None:
            return []
        try:
            with open(os.path.join(_dir, "journal-%04d.jsonl" % _seq),
                      "r", encoding="utf-8", errors="replace") as f:
                return f.read().splitlines()[-n:]
        except Exception:
            return []
