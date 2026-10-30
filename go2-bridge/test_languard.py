# -*- coding: utf-8 -*-
"""控制面鉴权回归(2026-10-29 审查加固)。

守两件事:
  1. 不配 allow/token 时行为与旧版完全一致(旧 G12 App 不带令牌照常能用) —— 这是
     最容易顺手弄坏的一条: 鉴权写错会把网络遥控整条通道静默废掉。
  2. 配了 token/allow 后, 不带令牌 / 来源不符的包必须被丢, 且不能污染 seq/统计。
"""
import json
import socket
import threading
import time

import netrc
from languard import NetGuard


def _send(port, obj):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(json.dumps(obj).encode(), ("127.0.0.1", port))
    s.close()


def _wait(pred, timeout=1.5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_guard_unit():
    g = NetGuard(allow=["192.168.8.0/24"], token="s3cret")
    assert g.ip_ok("192.168.8.8"), "白名单内 IP 应放行"
    assert not g.ip_ok("10.0.0.1"), "白名单外 IP 应拒"
    assert not g.ip_ok("not-an-ip"), "非法 IP 应拒"
    assert g.token_ok("s3cret"), "正确令牌应放行"
    assert not g.token_ok("wrong"), "错误令牌应拒"
    assert not g.token_ok(""), "空令牌应拒"

    opened = NetGuard()               # 两项都不配 = 完全开放(旧行为)
    assert opened.open and opened.check("203.0.113.9", None)
    print("test_languard: 单元断言通过")


def test_compat_no_auth():
    """不配鉴权: 旧格式包照旧生效"""
    port = 15511
    assert netrc.start(port, print, allow=[], token=""), "端口应能绑定"
    _send(port, {"seq": 1, "ch": [1002] * 16})
    ok = _wait(lambda: netrc.get() is not None)
    assert ok, "未配鉴权时旧格式包必须照常被接受(否则会静默断掉 App 通道)"
    print("test_languard: 兼容路径通过(旧 App 不受影响)")


def test_token_enforced():
    """配了令牌: 不带/带错令牌一律丢, 带对才收"""
    port = 15512
    assert netrc.start(port, print, allow=[], token="tk-123")
    _send(port, {"seq": 5, "ch": [1002] * 16})
    time.sleep(0.3)
    assert netrc.get() is None, "不带令牌的包不能生效"
    _send(port, {"seq": 6, "ch": [1002] * 16, "tok": "bad"})
    time.sleep(0.3)
    assert netrc.get() is None, "错误令牌的包不能生效"
    _send(port, {"seq": 7, "ch": [1600] * 16, "tok": "tk-123"})
    got = _wait(lambda: netrc.get() is not None)
    assert got and netrc.get()[0] == 1600, "正确令牌必须生效"
    assert netrc.snapshot()["net_rej"] >= 2, "拒绝计数应累计(调试页要看得见)"
    print("test_languard: 令牌门通过")


def test_allow_enforced():
    """白名单不含本机时, 来自本机的包必须被拒"""
    port = 15513
    assert netrc.start(port, print, allow=["10.99.99.0/24"], token="")
    _send(port, {"seq": 9, "ch": [1002] * 16})
    time.sleep(0.3)
    assert netrc.get() is None, "来源不在白名单必须丢"
    print("test_languard: 白名单门通过")


if __name__ == "__main__":
    test_guard_unit()
    test_compat_no_auth()
    test_token_enforced()
    test_allow_enforced()
    print("test_languard: 全部通过")
