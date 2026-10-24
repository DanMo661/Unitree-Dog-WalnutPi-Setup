# -*- coding: utf-8 -*-
"""网络遥控全链路仿真(无需硬件): App UDP 通道流 -> netrc -> rc_loop 状态机 -> Dog 指令桩
覆盖: 网络流优先于SBUS线 / App停发300ms后回退SBUS线 / 回退后实体遥控可重新接管 /
      全网断走失控保护 / 挡位同步 / net_ok 链路灯数据
用法: python run_test_shim.py test_netrc_sim.py   (不碰真实串口, 不向狗发指令)
"""
import json
import socket
import threading
import time

import bridge
import netrc

IDX = {'A': 4, 'C': 6, 'G': 9, 'P': 1}

# 网络流剧本: (起始秒, 通道覆盖 | 'STOP'=停发)
# 10-20 安全互锁: C 站起立放 5s 起立保护期外; 后续大动作间隔均 >= 2s
NET = [
    (1.0, {'A': 1002, 'C': 282, 'G': 282}),  # App 打开: 初始上锁/趴/挡1
    (2.0, {'A': 1722}),                      # 点解锁 -> 边沿
    (5.6, {'C': 1722}),                      # 点站立 -> StandUp(兜底到点后 BalanceStand)
    (9.4, {'P': 282}),                       # 摇杆上推满(App 语义 1002-720)
    (11.0, 'STOP'),                          # 切后台 -> 停发 -> 板端回退 SBUS 线
]

# 物理线剧本: 全程在场, 杆推满且方向与网络杆相反(验证网络优先不串台)
PHYS = [
    (0.0,  {'A': 1002, 'C': 282, 'P': 1722}),
    (12.6, {'A': 1722}),                     # 网络停发回退后, 实体翻拨重新接管
    (14.0, {'C': 1722}),                     # 实体 C 站起立(距回退趴下 2.7s, 过互锁)
    (15.0, 'STOP'),                          # 全断 -> 失控保护
]


def chans(script, t, default_c_down=True):
    """按时间线展开成 16 通道数组; 停发返回 None"""
    cur, stopped = {}, False
    for start, ev in script:
        if t >= start:
            if ev == 'STOP':
                stopped = True
            else:
                cur.update(ev)
                stopped = False
    if stopped:
        return None
    ch = [1002] * 16
    if default_c_down:
        ch[IDX['C']] = 282
    for k, v in cur.items():
        ch[IDX[k]] = v
    return ch


class StubLink:
    def __init__(self):
        self.cmds = []
        self.loop = True   # 探针判据同 DogLink(loop+state.link): 缺了影子恒判链路断

    def submit(self, name, **p):
        self.cmds.append((time.monotonic(), name, dict(p)))
        return True          # P0-3 起 submit 返回是否入队成功


class FakeSbus:
    """物理 SBUS 线桩: 与真实 Sbus.read 同构, 按 PHYS 时间线产帧"""

    def __init__(self, port, on_frames=None):
        self.t0 = time.monotonic()

    @property
    def opened(self):
        return True

    def open(self):
        return True

    def read(self):
        ch = chans(PHYS, time.monotonic() - self.t0)
        return None if ch is None else (ch, False, False)


def pump_udp(t0):
    """App 桩: 按 NET 时间线 50ms 一包 UDP:15010(与 ManualCtrlActivity 同节奏同格式)"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq = 0
    while True:
        ch = chans(NET, time.monotonic() - t0, default_c_down=False)
        if ch is None:
            break
        seq += 1
        sock.sendto(json.dumps({"seq": seq, "ch": ch}).encode(),
                    ("127.0.0.1", 15010))
        time.sleep(0.05)
    sock.close()


def find_cmds(stub, name, **match):
    out = []
    for _, n, p in stub.cmds:
        if n != name:
            continue
        ok = all(p.get(k) == v or (isinstance(v, float) and abs(p.get(k, 1e9) - v) < 1e-6)
                 for k, v in match.items())
        if ok:
            out.append(p)
    return out


def wait_until(fn, timeout, what):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if fn():
            return
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
    bridge.Sbus = FakeSbus                     # 注入物理线桩
    assert netrc.start(15010, print)           # 真实 netrc 接收器
    pa = bridge.Pa(30, print)

    t0 = time.monotonic()
    threading.Thread(target=bridge.rc_loop, args=(dog, pa), daemon=True).start()
    threading.Thread(target=pump_udp, args=(t0,), daemon=True).start()

    # 1. 网络流 A 初始停在上锁位(1002), 加上物理线也锁着 -> 不许解锁
    time.sleep(1.8)
    assert not state.rc_armed, '未翻拨就被解锁(边沿规则失效)'
    # 2. 网络点解锁(2.0 翻拨)
    wait_until(lambda: state.rc_armed, 2, '网络流边沿解锁')
    # 3. 网络点站立(5.6, 过 5s 起立保护期) -> StandUp + 走模式 + 挡1步态同步
    wait_until(lambda: state.mode == 2, 4, '网络C站进入走模式')
    assert find_cmds(stub, 'StandUp'), '缺 StandUp'
    wait_until(lambda: find_cmds(stub, 'FreeWalk'), 2, '挡1步态同步(10-21 起挡1=FreeWalk)')
    # 4. 起立兜底到点补 BalanceStand(仿真 STAND_TIMEOUT_S=1s, 真机 6s)
    wait_until(lambda: find_cmds(stub, 'BalanceStand'), 6, 'BalanceStand')
    # 5. 网络摇杆上推满(9.4, pending 已排空): 挡1限速 vx = axis(282)*SIGN_PITCH(-1)*1.0 = +1.0
    wait_until(lambda: find_cmds(stub, 'Move', x=1.0, y=0.0, z=0.0), 3, '网络满杆Move(+1.0, 挡1新限速)')
    #    物理线同期杆在反方向(1722), 若串台会出现 -1.0
    assert not find_cmds(stub, 'Move', x=-1.0), '物理线串台(网络优先失效)'
    # 6. net_ok 链路灯数据在发
    assert netrc.snapshot()["net_ok"] and netrc.snapshot()["net_seq"] > 0, 'net_ok 快照异常'
    # 7. App 停发(11.0) -> 300ms 后回退物理线(A=1002) -> 自动上锁+缓降
    #    (距 5.6 的 StandUp 5.7s, 过互锁)
    wait_until(lambda: not state.rc_armed, 2, '停发回退后上锁')
    wait_until(lambda: find_cmds(stub, 'StandDown'), 2, '回退缓降 StandDown')
    wait_until(lambda: not netrc.snapshot()["net_ok"], 2.5, 'net_ok 变灭')
    # 8. 实体翻拨(12.6)重新接管 -> 第二次解锁(距网络解锁 10.6s, 过 A 去抖)
    wait_until(lambda: state.rc_armed, 3, '实体重新接管解锁')
    # 9. 实体 C 站(14.0, 距回退趴下 2.7s 过互锁) -> 再起立
    wait_until(lambda: len(find_cmds(stub, 'StandUp')) >= 2, 3, '实体C站再起立')
    # 10. 全断(15.0) -> 失控保护: 停走保持姿态 + 超时上锁(09-29 起不再趴下; StandDown 仅第7步主动上锁那一次)
    wait_until(lambda: not state.rc_armed, 4, '总失联失控上锁')
    assert len(find_cmds(stub, 'StandDown')) == 1, '失控保护不应再发 StandDown'

    stat = {}
    for _, n, _ in stub.cmds:
        stat[n] = stat.get(n, 0) + 1
    print('全部断言通过')
    print('指令统计:', stat)


if __name__ == '__main__':
    main()
