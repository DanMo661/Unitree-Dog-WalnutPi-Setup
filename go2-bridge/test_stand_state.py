# -*- coding: utf-8 -*-
"""test_stand_state.py —— 状态驱动起立判定(bridge.Dog, 10-20 / P0-1 / P0-2)

背景: 第一次起立比后续慢, 旧代码 StandUp 后写死 3 秒补 BalanceStand, 会在
起立过程中插第二条指令, 把狗弹起来并触发对外供电口保护断电。现在改成:
看狗自报 body_height 的**不同遥测包**, 连续 0.6s 稳定且高于站姿阈值才补
BalanceStand; 没有姿态数据时 STAND_TIMEOUT_S(6s) 兜底; 有数据但一直低位
则判起立失败, 绝不补 BalanceStand(P0-2)。起立期间 move()/trick() 一律不响应。

用法: python run_test_shim.py test_stand_state.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bridge  # noqa: E402


class StubLink:
    def __init__(self, ok=True):
        self.cmds = []
        self.ok = ok

    def submit(self, name, **p):
        self.cmds.append((name, p))
        return self.ok


def names(stub):
    return [n for n, _ in stub.cmds]


def fresh(height, t):
    """模拟一条刚到站的 sportmodestate(height, 包时间戳 t)。"""
    bridge.state.dog_height = height
    bridge.state.dog_state_t = t


def main():
    # 1. 高度稳定 -> 提前补 BalanceStand(不等 6 秒兜底)
    stub = StubLink()
    d = bridge.Dog(stub, now=0.0)
    fresh(0.10, 5.0)
    assert d.set_mode(2, now=5.0) is True
    assert names(stub) == ["StopMove", "StandUp"], names(stub)
    assert d.rising is True
    d.move(0.1, 0.0, 0.0)
    assert "Move" not in names(stub), "起立中不得发 Move"
    fresh(0.15, 5.2); d._maybe_balance(5.2)
    fresh(0.25, 5.4); d._maybe_balance(5.4)
    fresh(0.25, 5.6); d._maybe_balance(5.6)
    assert "BalanceStand" not in names(stub), "2 个样本还不够判稳定"
    fresh(0.25, 5.8); d._maybe_balance(5.8)
    assert "BalanceStand" not in names(stub), "0.6s 窗口里还有起立途中的低值, 继续等"
    fresh(0.25, 6.0); d._maybe_balance(6.0)
    assert names(stub)[-1] == "BalanceStand", names(stub)
    assert d.rising is False

    # 1b. P0-1 回归: 同一个遥测包被 20Hz tick 反复读不得凑够 3 个样本提前判稳
    stub = StubLink()
    d = bridge.Dog(stub, now=0.0)
    fresh(0.30, 5.0)
    assert d.set_mode(2, now=5.0) is True
    for i in range(1, 11):                    # 0.5s 内同一包(ts 恒 5.0)反复 tick
        d._maybe_balance(5.0 + i * 0.05)
    assert "BalanceStand" not in names(stub), \
        "同一遥测包重复 tick 被当成多个样本 -> 起立稳定窗名存实亡(P0-1)"
    # 之后换真实的新包(10Hz), 首样本年龄够 0.5s 才判稳
    for i in range(1, 12):
        t = 5.0 + i * 0.05
        fresh(0.30, t)
        d._maybe_balance(t)
    assert names(stub)[-1] == "BalanceStand", names(stub)
    assert d.rising is False

    # 2. 起立完成后 move 恢复
    d.move(0.1, 0.0, 0.0)
    assert names(stub)[-1] == "Move", names(stub)

    # 3. 完全无姿态数据 -> STAND_TIMEOUT_S(6s)兜底, 不再是 3s; 告警只记一次
    stub = StubLink()
    d = bridge.Dog(stub, now=0.0)
    bridge.state.dog_height = None
    bridge.state.dog_state_t = None
    assert d.set_mode(2, now=5.0) is True
    warns = sum("无狗姿态遥测" in line for line in bridge._LOG_BUF)
    d._maybe_balance(5.0 + 2.9)
    assert "BalanceStand" not in names(stub), "无遥测不得在 3s 就补 BalanceStand(P0-2)"
    d._maybe_balance(5.0 + 5.9)
    assert "BalanceStand" not in names(stub)
    d._maybe_balance(5.0 + 6.0)
    assert names(stub)[-1] == "BalanceStand", names(stub)
    assert d.rising is False
    warns2 = sum("无狗姿态遥测" in line for line in bridge._LOG_BUF)
    assert warns2 == warns + 1, "无遥测告警应记且只记一次"
    # 同一次起立后续 tick 不再重复告警(限流)
    d._maybe_balance(5.0 + 6.1)
    assert sum("无狗姿态遥测" in line for line in bridge._LOG_BUF) == warns2

    # 4. 有数据但一直不稳(高位抖动) -> STAND_TIMEOUT_S 兜底(狗已在高位, 可以补)
    bridge.config.STAND_TIMEOUT_S = 6.0
    stub = StubLink()
    d = bridge.Dog(stub, now=0.0)
    assert d.set_mode(2, now=5.0) is True
    h = 0.25
    for i in range(1, 40):
        t = 5.0 + i * 0.2
        h = 0.25 if i % 2 else 0.31      # 一直抖, 判不了稳定
        fresh(h, t)
        d._maybe_balance(t)
        if "BalanceStand" in names(stub):
            break
    assert names(stub)[-1] == "BalanceStand", names(stub)
    assert abs((5.0 + 6.0) - t) < 0.25, "应在 STAND_TIMEOUT_S 附近兜底, 实际 %s" % t

    # 5. P0-2: 到 deadline 高度仍低于站姿阈值 -> 取消起立, 绝不补 BalanceStand
    stub = StubLink()
    d = bridge.Dog(stub, now=0.0)
    fresh(0.10, 5.0)
    assert d.set_mode(2, now=5.0) is True
    for i in range(1, 14):               # 5.0 -> 11.5s, 一直趴着的高度
        t = 5.0 + i * 0.5
        fresh(0.10, t)
        d._maybe_balance(t)
    assert "BalanceStand" not in names(stub), \
        "狗没起立却补 BalanceStand(半起的狗会被弹起来, P0-2)"
    assert d.rising is False and d.mode == 0
    assert bridge.state.mode == 0, "起立失败后本地模式必须同步回趴"
    assert bridge._EVENTS and bridge._EVENTS[-1]["ev"] == "stand_failed", \
        list(bridge._EVENTS)[-1:]

    # 6. P1: 坐姿(mode3)取消起立流程, 之后不再补 BalanceStand
    stub = StubLink()
    d = bridge.Dog(stub, now=0.0)
    fresh(0.30, 5.0)
    assert d.set_mode(2, now=5.0) is True
    d.mode = 3
    n = len(stub.cmds)
    d._maybe_balance(5.1)
    assert d.rising is False
    d._maybe_balance(6.0)
    assert len(stub.cmds) == n, "坐姿不得补 BalanceStand"

    # 7. P1: 起立中 trick 一律拒绝
    bridge.state.link = "up"
    stub = StubLink()
    d = bridge.Dog(stub, now=0.0)
    fresh(0.10, 5.0)
    assert d.set_mode(2, now=5.0) is True
    assert d.trick("Hello", 4) is False, "起立中不得插动作"
    assert "Hello" not in names(stub)

    # 8. P0-3: submit 失败不提交本地状态(供 rc_loop 每帧重试)
    stub = StubLink(ok=False)
    d = bridge.Dog(stub, now=0.0)
    fresh(0.30, 5.0)
    state_before = bridge.state.mode
    assert d.set_mode(2, now=5.0) is False, "入队失败应返回 False"
    assert d.mode == 0 and d.rising is False
    assert bridge.state.mode == state_before, "入队失败不得改本地状态镜像"
    assert d.move(0.1, 0.0, 0.0) is None
    assert d.moving is False, "Move 入队失败不得认账"
    d.mode = 2
    assert d.move(0.1, 0.0, 0.0) is None
    assert d.moving is False

    print("test_stand_state: 10 组断言全部通过")


if __name__ == "__main__":
    main()
