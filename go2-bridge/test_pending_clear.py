# -*- coding: utf-8 -*-
"""回归: 模式切换必须作废 pending 延迟指令.
10-17 实测事故: C 拨上起立(StandUp+3s后补BalanceStand), 3s 内又拨 C 趴下,
迟到的 BalanceStand 把趴着的狗瞬间弹起. 本测试锁定该修复."""
import time

import run_test_shim  # noqa: F401  (本机无 audioop 时垫片)
import bridge


class StubLink:
    def __init__(self):
        self.cmds = []

    def submit(self, name, **p):
        self.cmds.append((name, dict(p)))


def main():
    stub = StubLink()
    d = bridge.Dog(stub)
    d.mode = 0
    d.set_mode(2)                          # 0->2: StandUp + 挂 3s 后 BalanceStand
    assert ('StandUp', {}) in stub.cmds, '应发 StandUp'
    d.set_mode(0)                          # 3s 内趴下 -> pending 必须作废
    assert ('StandDown', {}) in stub.cmds, '应发 StandDown'
    time.sleep(3.2)                        # 越过原触发点
    d.tick()                               # 到点检查
    assert not any(c[0] == 'BalanceStand' for c in stub.cmds), \
        '迟到的 BalanceStand 发给了趴着的狗(瞬间弹起 bug 回归)'
    # 正常路径不受影响: 趴->站后不被打断, 3s 后 BalanceStand 照发
    stub2 = StubLink()
    d2 = bridge.Dog(stub2)
    d2.mode = 0
    d2.set_mode(2)
    time.sleep(3.2)
    d2.tick()
    assert any(c[0] == 'BalanceStand' for c in stub2.cmds), \
        '正常起立的补 BalanceStand 不应被误伤'
    print('pending 清除回归通过: 打断路径无迟到弹起 / 正常路径补发完好')


if __name__ == '__main__':
    main()
