# -*- coding: utf-8 -*-
"""全链路软件仿真(无需硬件): 合成遥控时序 -> rc_loop 状态机 -> Dog 指令桩
覆盖: 边沿解锁/重锁、站趴模式、三档步态+分挡限速、Move 流、B 键动作、失控保护、恢复再解锁
用法: .venv/bin/python test_link_sim.py   (不碰真实串口, 不向狗发指令)
"""
import threading
import time

import bridge

IDX = {'A': 4, 'B': 5, 'C': 6, 'D': 8, 'G': 9, 'P': 1, 'R': 3, 'Y': 0}

# 脚本时间线: (起始秒, 通道覆盖字典 | 'STOP'=信号中断)
# 10-20 安全互锁: C 站起立放 5s 起立保护期外; C 趴->站反向间隔 >= 2s(不够则被拦,
# rc_loop 每帧重试到点自动起立); 第二次解锁距第一次 >= 1s(A 去抖)
SCRIPT = [
    (0.0, {'A': 1722, 'G': 282}),  # 开机 A 停在解锁位 + 挡位在挡1 -> 边沿规则必须拒绝
    (0.6, {'A': 1002}),            # 拨回上锁位
    (1.0, {'A': 1722}),            # 翻上 -> 此刻才允许解锁(此时 C 在趴位)
    (5.5, {'C': 1722}),            # C 站 -> 起立(过了 5s 起立保护期)
    (6.1, {'G': 1722}),            # 挡3 -> TrotRun
    (6.7, {'P': 1722}),            # 前后杆推满(挡3 限速 2.5; BalanceStand 后生效)
    (9.1, {'P': 1002}),            # 回中 -> StopMove
    (9.7, 'STOP'),                 # 信号中断 -> 失控保护
    (11.7, {'A': 1002}),           # 信号恢复, A 在下位
    (12.1, {'A': 1722}),           # 翻上 -> 第二次解锁(距第一次 11.1s, 过 A 去抖)
    (15.3, {'B': 1722}),           # B 拨到高位 -> 10-17 起为继电器输出, 不再触发动作
    (15.7, {'D': 1722}),           # D 拨到高位 -> 已退役, 不应触发任何动作
    (19.7, {'C': 282}),            # C 拨趴(距起立 14.2s, 过互锁)
    (20.1, {'C': 1722}),           # C 拨回 -> 互锁拦(距趴 0.4s), 每帧重试 ~21.7s 到点再起立
]


class StubLink:
    """Dog 发送桩: 只记录, 不出门"""

    def __init__(self):
        self.cmds = []
        self.loop = True   # 探针判据同 DogLink(loop+state.link): 缺了影子恒判链路断

    def submit(self, name, **p):
        self.cmds.append((name, dict(p)))
        return True          # P0-3 起 submit 返回是否入队成功


class FakeSbus:
    """按时间线产 SBUS 帧解码结果, 与真实 Sbus.read 同构"""

    def __init__(self, port, on_frames=None):
        self.t0 = time.monotonic()

    @property
    def opened(self):
        return True

    def open(self):
        return True

    def read(self):
        t = time.monotonic() - self.t0
        cur, stopped = {}, False
        for start, ev in SCRIPT:
            if t >= start:
                if ev == 'STOP':
                    stopped = True
                else:
                    cur.update(ev)
                    stopped = False
        if stopped:
            return None
        ch = [1002] * 16
        ch[IDX['C']] = 282                     # C 默认趴位(真实拨档低位 282)
        for k, v in cur.items():
            ch[IDX[k]] = v
        return ch, False, False


def find_cmds(stub, name, **match):
    out = []
    for n, p in stub.cmds:
        if n != name:
            continue
        ok = True
        for k, v in match.items():
            if isinstance(v, float):
                ok = ok and abs(p.get(k, 1e9) - v) < 1e-6
            else:
                ok = ok and p.get(k) == v
        if ok:
            out.append(p)
    return out


def wait_until(fn, timeout, what):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if fn():
            return True
        time.sleep(0.05)
    raise AssertionError('超时: ' + what)


def main():
    stub = StubLink()
    dog = bridge.Dog(stub, now=time.monotonic())   # 传锚点: 启用互锁+起立保护期(10-20)
    state = bridge.state
    state.link = 'up'
    state.link_gen = 0
    bridge.config.FS_DAMP_S = 1.5              # 仿真缩短真失联判定(真机30s), 代码路径相同
    bridge.config.STAND_TIMEOUT_S = 1.0        # 仿真缩短无遥测兜底(真机6s), 代码路径相同
    bridge.Sbus = FakeSbus                     # 注入仿真串口
    pa = bridge.Pa(30, print)

    threading.Thread(target=bridge.rc_loop, args=(dog, pa), daemon=True).start()

    # 1. 边沿规则: 开机 A 停解锁位, 1.0s 前绝不允许解锁
    time.sleep(0.9)
    assert not state.rc_armed, 'A 停在解锁位被误解锁(边沿规则失效)'
    # 2. 翻拨后解锁
    wait_until(lambda: state.rc_armed, 2, '翻拨解锁')
    # 3. C 站(5.5s, 过起立保护期) -> 走模式 + StandUp；10-21 起立期间不得发步态
    wait_until(lambda: state.mode == 2, 5, 'C站进入走模式')
    assert find_cmds(stub, 'StandUp'), '缺 StandUp'
    # 4. 起立兜底到点补 BalanceStand(仿真 STAND_TIMEOUT_S=1s, 真机 6s)；
    #    步态必须在 BalanceStand 之后才同步(黑匣子: 起立瞬间并发 EconomicGait 触发上装断电)
    wait_until(lambda: find_cmds(stub, 'BalanceStand'), 6, 'BalanceStand')
    names = [n for n, _p in stub.cmds]
    gait_idx = [names.index(n) for n in
                ('EconomicGait', 'FreeWalk', 'TrotRun') if n in names]
    assert gait_idx and min(gait_idx) > names.index('BalanceStand'), \
        '起立期间发了步态指令 / 起立后未补步态: %s' % names
    # 5. 挡3 步态(起立后按当前挡位补同步)
    wait_until(lambda: find_cmds(stub, 'TrotRun'), 2, '挡3步态')
    # 6. 推满杆: 起立 rising 解除后生效: vx = axis(1722) * SIGN_PITCH(-1) * 挡3上限3.0 = -3.0(10-21 提速)
    wait_until(lambda: find_cmds(stub, 'Move', x=-3.0, y=0.0, z=0.0), 4, '挡3满杆Move(-3.0)')
    # 7. 回中 -> 新的 StopMove(不能匹配起立前那条, 否则后面的失联窗口整体错位)
    n_stop = len(find_cmds(stub, 'StopMove'))
    wait_until(lambda: len(find_cmds(stub, 'StopMove')) > n_stop, 4, '回中停走')
    # 8. 失控: 短时丢帧 hold 站定, 超过 FS_DAMP_S(仿真1.5s) 上锁停走(09-29 起不再趴下, 保持姿态)
    wait_until(lambda: not state.rc_armed, 4, '失控上锁')
    assert not find_cmds(stub, 'StandDown'), '失控保护不应再趴下(应保持姿态)'
    # 9. 恢复信号 + 再翻拨(距第一次解锁 11.1s, 过 A 去抖) -> 第二次解锁
    wait_until(lambda: state.rc_armed, 4, '恢复后再解锁')
    # 10. B/D 拨到高位都不应再触发动作(B=继电器/D=退役); TRICKS 只剩 App 入口
    time.sleep(4.5)
    assert not find_cmds(stub, 'Hello'), 'B 键不应再触发 Hello(已让位继电器)'
    assert not find_cmds(stub, 'Dance1'), 'D 键不应触发 Dance1(已退役)'
    # 11. C 趴(19.7s) -> C 站(20.1s): 反向间隔 0.4s 被互锁拦, 每帧重试到点(约 21.7s)自动再起立
    #     (共2次 StandUp: 5.5s 首次 + 互锁到点重放; 失联不再趴下后, 恢复解锁无需重新起立)
    wait_until(lambda: len(find_cmds(stub, 'StandUp')) >= 2, 8, 'C趴后C站再起立(互锁到点重试)')
    time.sleep(1.5)
    assert not find_cmds(stub, 'Dance1'), 'D 键已改为喊话开关, 不应触发动作'
    assert not find_cmds(stub, 'Sit'), '拨杆不应触发动作'

    stat = {}
    for n, _ in stub.cmds:
        stat[n] = stat.get(n, 0) + 1
    print('全部断言通过')
    print('指令统计:', stat)


if __name__ == '__main__':
    main()
