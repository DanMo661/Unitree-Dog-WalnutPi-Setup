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
  close()                                 可选，关当前文件
线程安全：内部 threading.Lock 串行化。
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

_lock = threading.Lock()
_fh = None          # 当前打开的日志文件对象；None = 未 init / 已关
_dir = None         # 实际使用的目录
_seq = 0            # 当前文件序号
_bytes = 0          # 当前文件已写字节（含 init 续接的旧内容）
_max_bytes = 32 * 1024 * 1024


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
            _bytes += nbytes
        except Exception:
            pass


def close():
    """关闭当前文件。可再次 init 重开。"""
    global _fh
    with _lock:
        if _fh is not None:
            try:
                _fh.close()
            except Exception:
                pass
            _fh = None
