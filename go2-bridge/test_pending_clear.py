# -*- coding: utf-8 -*-
"""回归: 模式切换必须作废未决起立流程(FsmActive/DogFsm, 旧 Dog 已删 ADR-0004).
10-17 实测事故: C 拨上起立(StandUp+兜底补BalanceStand), 补发前又拨 C 趴下,
迟到的 BalanceStand 把趴着的狗瞬间弹起. 本测试锁定该修复."""
import time

import run_test_shim  # noqa: F401  (本机无 audioop 时垫片)
import bridge


class StubLink:
    """FSM 指令桩(send 注入 FsmActive): 只记录, 不出门"""

    def __init__(self):
        self.cmds = []

    def submit(self, name, **p):
        self.cmds.append((name, dict(p)))
        return True          # P0-3 起 submit 返回是否入队成功


def main():
    bridge.state.dog_height = None      # 无姿态遥测: 走 STAND_TIMEOUT_S 兜底
    bridge.state.dog_state_t = None
    stub = StubLink()
    d = bridge.FsmActive(stub)
    d.fsm._t0 = time.monotonic() - 100.0   # 锚点拨回: 过起立保护期(模拟开机已久)
    d.fsm.mode = 0
    d.fsm.set_mode(2)                      # 0->2: StandUp 并进入 rising
    assert ('StandUp', {}) in stub.cmds, '应发 StandUp'
    d.fsm.set_mode(0)                      # 起立中趴下 -> rising 必须作废
    assert ('StandDown', {}) in stub.cmds, '应发 StandDown'
    d.fsm.rise_deadline = time.monotonic() # 把兜底拨到当下(免等 6s)
    d.tick(time.monotonic())               # 到点检查(现役接线: FSM 从 state 读遥测)
    assert not any(c[0] == 'BalanceStand' for c in stub.cmds), \
        '迟到的 BalanceStand 发给了趴着的狗(瞬间弹起 bug 回归)'
    # 正常路径不受影响: 趴->站后不被打断, 到点 BalanceStand 照发
    stub2 = StubLink()
    d2 = bridge.FsmActive(stub2)
    d2.fsm._t0 = time.monotonic() - 100.0
    d2.fsm.mode = 0
    d2.fsm.set_mode(2)
    assert d2.fsm.rising is True
    d2.fsm.rise_deadline = time.monotonic()  # 同上: 直接越过兜底时刻
    d2.tick(time.monotonic())
    assert any(c[0] == 'BalanceStand' for c in stub2.cmds), \
        '正常起立的补 BalanceStand 不应被误伤'
    print('pending 清除回归通过: 打断路径无迟到弹起 / 正常路径补发完好')


if __name__ == '__main__':
    main()
