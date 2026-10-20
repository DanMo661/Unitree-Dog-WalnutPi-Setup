# -*- coding: utf-8 -*-
"""test_stand_state.py —— 状态驱动起立判定(bridge.Dog, 10-20)

背景: 第一次起立比后续慢, 旧代码 StandUp 后写死 3 秒补 BalanceStand, 会在
起立过程中插第二条指令, 把狗弹起来并触发对外供电口保护断电。现在改成:
看狗自报 body_height, 连续 0.6s 稳定且高于站姿阈值才补 BalanceStand;
没有姿态数据时回落旧行为(3 秒); 有数据但一直不稳则 STAND_TIMEOUT_S 兜底。
起立期间 move() 一律不响应。

用法: python run_test_shim.py test_stand_state.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bridge  # noqa: E402


class StubLink:
    def __init__(self):
        self.cmds = []

    def submit(self, name, **p):
        self.cmds.append((name, p))


def names(stub):
    return [n for n, _ in stub.cmds]


def fresh(height, t):
    """模拟一条刚到站的 sportmodestate(height, 时刻 t)。"""
    bridge.state.dog_height = height
    bridge.state.dog_state_t = t


def main():
    # 1. 高度稳定 -> 提前补 BalanceStand(不等 3 秒兜底)
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

    # 2. 起立完成后 move 恢复
    d.move(0.1, 0.0, 0.0)
    assert names(stub)[-1] == "Move", names(stub)

    # 3. 无姿态数据 -> 3 秒兜底(与旧行为一致)
    stub = StubLink()
    d = bridge.Dog(stub, now=0.0)
    bridge.state.dog_height = None
    bridge.state.dog_state_t = None
    assert d.set_mode(2, now=5.0) is True
    d._maybe_balance(5.0 + 2.9)
    assert "BalanceStand" not in names(stub)
    d._maybe_balance(5.0 + 3.0)
    assert names(stub)[-1] == "BalanceStand", names(stub)
    assert d.rising is False

    # 4. 有数据但一直不稳 -> STAND_TIMEOUT_S 兜底
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

    # 5. 趴下会取消起立流程, 之后不得再补 BalanceStand
    stub = StubLink()
    d = bridge.Dog(stub, now=0.0)
    assert d.set_mode(2, now=5.0) is True
    assert d.set_mode(0, now=7.1) is True      # 过了 2s 互锁
    assert d.rising is False
    n = len(stub.cmds)
    d._maybe_balance(20.0)
    assert len(stub.cmds) == n, "趴下后不得再补 BalanceStand"

    print("test_stand_state: 5 组断言全部通过")


if __name__ == "__main__":
    main()
