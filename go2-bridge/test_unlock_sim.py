# -*- coding: utf-8 -*-
"""解锁弹起修复专项仿真(无需硬件): 趴着的狗解锁时 C 原本就在上, 不再自动起立
覆盖: 开机站立解锁即可控(不回退)、上锁趴下后再解锁不弹起、C 翻拨边沿起立、
      趴着切挡只记账起立补发(步态指令不把趴着的狗弹起来)
用法: python run_test_shim.py test_unlock_sim.py
"""
import threading
import time

import bridge

IDX = {'A': 4, 'B': 5, 'C': 6, 'D': 8, 'G': 9, 'P': 1, 'R': 3, 'Y': 0}

# 脚本时间线: (起始秒, 通道覆盖字典)。C/G 基态=上位/挡3(见 FakeSbus BASE)
SCRIPT = [
    (0.0, {'A': 1002}),   # 上锁
    (0.5, {'A': 1722}),   # 解锁#1: 狗站着(模拟开机首连 mode=1) -> 只补 BalanceStand, 不 StandUp
    (3.0, {'A': 1002}),   # 上锁: 狗趴下(StandDown)
    (3.5, {'A': 1722}),   # 解锁#2: C 还在上, 狗趴着 -> 不许弹起!(本次修复的核心断言)
    (3.8, {'G': 282}),    # 趴着切挡1 -> 只记账, 不发 EconomicGait
    (4.5, {'C': 282}),    # C 拨下(趴着, 无指令)
    (5.0, {'C': 1722}),   # C 下->上边沿 -> 这才起立(StandUp)
]


class StubLink:
    """Dog 发送桩: 只记录, 不出门"""

    def __init__(self):
        self.cmds = []

    def submit(self, name, **p):
        self.cmds.append((name, dict(p)))


class FakeSbus:
    """按时间线产 SBUS 帧解码结果, 与真实 Sbus.read 同构; C 基态=上位"""

    def __init__(self, port, on_frames=None):
        self.t0 = time.monotonic()

    @property
    def opened(self):
        return True

    def open(self):
        return True

    def read(self):
        t = time.monotonic() - self.t0
        cur = {}
        for start, ev in SCRIPT:
            if t >= start:
                cur.update(ev)
        ch = [1002] * 16
        ch[IDX['C']] = 1722        # C 基态=上位(区别于 test_link_sim 的趴位基态)
        ch[IDX['G']] = 1722        # 挡3
        for k, v in cur.items():
            ch[IDX[k]] = v
        return ch, False, False


def find_cmds(stub, name):
    return [p for n, p in stub.cmds if n == name]


def wait_until(fn, timeout, what):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if fn():
            return True
        time.sleep(0.05)
    raise AssertionError('超时: ' + what)


def main():
    stub = StubLink()
    dog = bridge.Dog(stub)
    dog.mode = 1                 # 模拟开机首连: 狗自己站着, 锁定等解锁(bridge reset() 行为)
    state = bridge.state
    state.link = 'up'
    state.link_gen = 0
    bridge.Sbus = FakeSbus        # 注入仿真串口
    pa = bridge.Pa(30, print)

    threading.Thread(target=bridge.rc_loop, args=(dog, pa), daemon=True).start()

    # 1. 开机站立 + C 在上 + 解锁: 直接可控(模式 1->2, 不发 StandUp/BalanceStand)
    wait_until(lambda: state.rc_armed, 2, '解锁#1')
    wait_until(lambda: state.mode == 2, 2, '解锁后进入走模式(可控)')
    assert not find_cmds(stub, 'StandUp'), '站着的狗解锁不该发 StandUp'
    # 挡3 在站立状态同步
    wait_until(lambda: find_cmds(stub, 'TrotRun'), 2, '站立时挡3步态')

    # 2. 上锁 -> 趴下
    wait_until(lambda: find_cmds(stub, 'StandDown'), 3, '上锁趴下')
    wait_until(lambda: not state.rc_armed, 2, '已上锁')
    wait_until(lambda: state.mode == 0, 2, '模式回到趴')

    # 3. 解锁#2(核心): C 停在上位, 狗趴着 -> 绝不起立、不补步态
    wait_until(lambda: state.rc_armed, 3, '解锁#2')
    time.sleep(1.0)              # 走过解锁后若干周期, 有弹起 bug 的话这里早就 StandUp 了
    assert not find_cmds(stub, 'StandUp'), '趴着解锁被弹起(修复失效)'
    assert state.mode == 0, '趴着解锁不该改模式'

    # 4. 趴着切挡1(3.8s 起): 只记账(此刻 ~4.5s, 已走过十余个周期)
    assert not find_cmds(stub, 'EconomicGait'), '趴着切挡发了步态指令(会把狗弹起来)'

    # 5. C 拨下再拨上: 边沿起立; 起立后挡1 补发; 3s 后补 BalanceStand(pending 路径)
    wait_until(lambda: find_cmds(stub, 'StandUp'), 4, 'C 翻拨边沿起立')
    wait_until(lambda: state.mode == 2, 2, '起立进走模式')
    wait_until(lambda: find_cmds(stub, 'EconomicGait'), 3, '起立后补发挡1步态')
    wait_until(lambda: find_cmds(stub, 'BalanceStand'), 5, '起立3s后补BalanceStand')
    time.sleep(1.0)
    assert len(find_cmds(stub, 'StandUp')) == 1, '全程只允许一次 StandUp(C 边沿那次)'

    stat = {}
    for n, _ in stub.cmds:
        stat[n] = stat.get(n, 0) + 1
    print('全部断言通过')
    print('指令统计:', stat)


if __name__ == '__main__':
    main()
