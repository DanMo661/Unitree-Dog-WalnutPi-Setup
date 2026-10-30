# -*- coding: utf-8 -*-
"""网络遥控接收: UDP:15010 收 G12 App 轮询到的全通道舵量数组, 等效替代 SBUS 线
包格式(JSON): {"seq":N,"ch":[c0..c11]}  原始舵量 int, 与 GR01 SBUS 输出同语义
            (可选 {"tok":"..."}: config.NETRC_TOKEN 非空时必带)
netrc.get(): 300ms 内有包返回最新通道数组, 否则 None(rc_loop 回退 SBUS 线)
链路丢包靠 seq 检测(统计给调试页), 控制安全由 rc_loop 现有失控保护兜底
权衡(设计内): 停发后 0~300ms 内桥按最后杆量盲发(FRESH_S 缓存), 真停走延迟
≈0.3+0.25=0.55s; 高丢包下偶发顿挫——20Hz 要 7 连丢(>300ms 空窗)才触发"""
import json
import socket
import threading
import time

from languard import NetGuard

FRESH_S = 0.3          # 300ms 内有包算新鲜(App 20Hz, 容忍连丢 5 包)
LOSS_TIMEOUT_S = 1.0   # 1s 无包=网络遥控失联(调试页红灯)

_state = {"ch": None, "t": 0.0, "seq": -1, "rx": 0, "lost": 0, "addr": ""}
_lock = threading.Lock()
_guard = NetGuard()


def start(port, log=print, allow=None, token=""):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
    except Exception as e:
        log("网络遥控端口 %d 被占, 停用: %r" % (port, e))
        return False
    sock.settimeout(0.5)
    global _guard
    _guard = NetGuard(allow=allow, token=token, log=log, name="网络遥控")
    _guard.assert_configured(log, "config.py 配 NETRC_ALLOW / NETRC_TOKEN 可只放行自己的 App")
    threading.Thread(target=_loop, args=(sock, log), daemon=True).start()
    log("网络遥控就绪: UDP :%d (G12 App 杆量通道, 新鲜时优先于 SBUS 线)" % port)
    return True


def _loop(sock, log):
    last_seq = -1
    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except Exception:
            continue
        try:
            m = json.loads(data.decode("utf-8"))
            ch = [int(v) for v in m.get("ch", [])]
            if len(ch) < 12:
                continue
            seq = int(m.get("seq", -1))
        except Exception:
            continue
        if not _guard.check(addr[0], m.get("tok")):
            continue
        with _lock:
            if last_seq >= 0 and seq > last_seq + 1:
                # 丢包是链路常态, 只累计给调试页; 封顶防 seq 大跳变把统计灌爆(纯计数, 控制无影响)
                _state["lost"] += min(seq - last_seq - 1, 100)
            last_seq = seq
            _state.update(ch=ch[:16], t=time.monotonic(), seq=seq,
                          rx=_state["rx"] + 1, addr="%s:%d" % addr)


def snapshot():
    """供 /ws//state 推送: 网络遥控链路灯(调试页显示) + 最近通道数组。
    ch 供 /state 的 ch_effective 用(netrc 生效时控制输入就是这份通道值)。"""
    with _lock:
        alive = _state["ch"] is not None and time.monotonic() - _state["t"] < LOSS_TIMEOUT_S
        return {"net_ok": alive, "net_rx": _state["rx"], "net_lost": _state["lost"],
                "net_seq": _state["seq"], "net_rej": _guard.rej,
                "ch": list(_state["ch"]) if _state["ch"] is not None else None}


def get():
    """最新通道数组(SBUS 同语义); 不新鲜返回 None"""
    with _lock:
        if _state["ch"] is not None and time.monotonic() - _state["t"] < FRESH_S:
            return list(_state["ch"])
    return None
