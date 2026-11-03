# -*- coding: utf-8 -*-
"""安全互锁单元测试(无需硬件): FsmActive/DogFsm.set_mode 返回值 / 大动作互锁计时 / 起立保护期
背景: 10-20 供电口事故 —— 狗对上装板供电, 起立瞬间狗对外供电口保护性断电板子重启,
两个大动作(StandDown <-> StandUp)间隔太近是触发条件之一(见 config.py 10-20 注释)。
时基注入: FSM 事件全收 now 参数(d.fsm.set_mode(m, now=t)); 锚点 = FsmActive(link, now=)
构造入参, 互锁锚点经 d.fsm._t0 / d.fsm._last_big 拨动 —— 不 sleep、不碰线程。
旧 Dog 的拦截 episode 记账(_blk_*/unblock/req_cancel 事件)随旧路径退役(ADR-0004):
active 下被拦 = set_mode 返回 False, 无 episode 状态可断言。
用法: python run_test_shim.py test_safety.py
"""
import time

import bridge


class StubLink:
    """FSM 指令桩(send 注入 FsmActive): 只记录, 不出门"""

    def __init__(self):
        self.cmds = []

    def submit(self, name, **p):
        self.cmds.append((name, dict(p)))
        return True          # P0-3 起 submit 返回是否入队成功


def names(stub):
    return [n for n, _ in stub.cmds]


def main():
    # 1. 起立保护期: 锚点(进程启动)后 STAND_GUARD_S(5s) 内拒绝会发 StandUp 的请求
    stub = StubLink()
    d = bridge.FsmActive(stub, now=0.0)
    assert d.fsm.set_mode(2, now=2.0)[0] is False, "保护期内起立应被拦"
    assert not names(stub), "被拦时不得发任何指令(含 StopMove)"
    assert d.fsm.set_mode(2, now=4.9)[0] is False, "4.9s 仍在保护期"
    assert d.fsm.set_mode(2, now=5.0)[0] is True, "到点(>=5s, 不小于)应放行"
    assert names(stub) == ["StopMove", "StandUp"], names(stub)

    # 2. 保护期只拦起立方向: 保护期内趴下(StandDown)不受影响
    stub = StubLink()
    d = bridge.FsmActive(stub, now=0.0)
    d.fsm.mode = 2
    assert d.fsm.set_mode(0, now=1.0)[0] is True, "保护期内趴下不应被拦"
    assert names(stub) == ["StopMove", "StandDown"], names(stub)

    # 3. 上锁/趴下永不拦(收权优先): 起立后立刻趴下也必须放行并发出指令;
    #    紧接着的再次起立才被互锁拦下, 到点放行(10-20 复核抓出原实现会吞上锁)
    stub = StubLink()
    d = bridge.FsmActive(stub, now=0.0)
    assert d.fsm.set_mode(2, now=5.0)[0] is True          # 起立(_last_big=5.0)
    assert d.fsm.set_mode(0, now=5.2)[0] is True, "上锁/趴下任何时候都必须放行"
    assert names(stub) == ["StopMove", "StandUp", "StopMove", "StandDown"], names(stub)
    n0 = len(stub.cmds)
    assert d.fsm.set_mode(2, now=5.3)[0] is False, "趴下后 2s 内起立应被拦"
    assert len(stub.cmds) == n0, "被拦期间不得发任何指令"
    assert d.fsm.set_mode(2, now=7.2)[0] is True, "距趴下整 2.0s 应放行"
    assert names(stub)[-2:] == ["StopMove", "StandUp"], names(stub)

    # 4. 互锁双向: 趴下后 2s 内起立同样被拦(保护期已过才测得到这个方向)
    stub = StubLink()
    d = bridge.FsmActive(stub, now=0.0)
    d.fsm.mode = 2
    assert d.fsm.set_mode(0, now=5.0)[0] is True          # 趴下(_last_big=5.0)
    assert d.fsm.set_mode(2, now=6.0)[0] is False, "1.0s 内起立应被拦"
    assert d.fsm.set_mode(2, now=7.25)[0] is True, "2.25s 应放行"
    assert names(stub) == ["StopMove", "StandDown", "StopMove", "StandUp"], names(stub)

    # 5. 非大动作不受互锁/保护期: 1->2 只停走(走模式由 Move 流驱动); 2->1 补
    #    BalanceStand(不发 StandUp); 3->0 坐->趴静默 —— 都不在 0<->站走 切换集内
    stub = StubLink()
    d = bridge.FsmActive(stub, now=0.0)
    d.fsm.mode = 1
    assert d.fsm.set_mode(2, now=1.0)[0] is True, "1->2 非大动作不应被拦"
    assert names(stub) == ["StopMove"], names(stub)
    assert d.fsm.set_mode(1, now=1.5)[0] is True, "2->1 非大动作不应被拦"
    assert names(stub) == ["StopMove", "StopMove", "BalanceStand"], names(stub)
    d.fsm.mode = 3
    stub.cmds.clear()
    assert d.fsm.set_mode(0, now=2.0)[0] is True, "3->0 坐->趴静默不应被拦"
    assert not names(stub), "坐->趴静默不得发指令"

    # 6. 坐姿规则(P1 去重): mode3 时请求 2 返回 False 且不发任何指令,
    #    不再每帧塞一条 StopMove(20Hz 刷指令队列)
    stub = StubLink()
    d = bridge.FsmActive(stub, now=0.0)
    d.fsm.mode = 3
    assert d.fsm.set_mode(2, now=6.0)[0] is False, "坐着时 C 在上位不算数"
    assert names(stub) == [], names(stub)
    assert d.fsm.mode == 3, "坐姿拦截不得改模式"

    # 7. 同模式幂等: 返回 True 且无指令
    stub = StubLink()
    d = bridge.FsmActive(stub, now=0.0)
    d.fsm.mode = 2
    assert d.fsm.set_mode(2, now=6.0)[0] is True
    assert not names(stub), "幂等请求不得发指令"

    # 8. 被拦的请求不刷新互锁锚点(否则锚点被越推越远, 永远到不了点)
    #    互锁只拦起立: 先起立再立刻趴下(放行, 锚点=5.1), 随后起立被拦且不刷新锚点
    stub = StubLink()
    d = bridge.FsmActive(stub, now=0.0)
    assert d.fsm.set_mode(2, now=5.0)[0] is True          # 起立(_last_big=5.0)
    assert d.fsm.set_mode(0, now=5.1)[0] is True          # 趴下永远放行(_last_big=5.1)
    assert d.fsm.set_mode(2, now=5.5)[0] is False         # 被拦: 不得刷新锚点
    assert d.fsm.set_mode(2, now=6.0)[0] is False
    assert d.fsm.set_mode(2, now=7.1)[0] is True, "以趴下(5.1)为锚, 整 2s 放行"

    # 9. 缺省 now = time.monotonic(): 真实时基下新锚点仍在保护期内
    stub = StubLink()
    d = bridge.FsmActive(stub, now=time.monotonic())
    assert d.set_mode(2) is False, "刚锚定的缺省 now 应落在保护期内"
    assert not names(stub)
    d.fsm._t0 = time.monotonic() - 100.0           # 把锚点拨回 100s 前(模拟开机已久)
    assert d.set_mode(2) is True, "锚点拨回后缺省 now 应放行"

    # 10. 起立流程中上锁/趴下立即生效(不拦), 且取消起立判定; 随后起立按互锁等待
    stub = StubLink()
    d = bridge.FsmActive(stub, now=0.0)
    assert d.fsm.set_mode(2, now=5.0)[0] is True          # 进入起立流程(rising)
    assert d.fsm.rising is True
    assert d.fsm.set_mode(0, now=5.5)[0] is True, "起立中上锁也必须放行(收权优先)"
    assert d.fsm.rising is False, "趴下必须取消起立流程"
    assert names(stub)[-2:] == ["StopMove", "StandDown"], names(stub)
    assert d.fsm.set_mode(2, now=6.0)[0] is False, "紧接着的起立仍受互锁拦"
    assert d.fsm.set_mode(2, now=7.5)[0] is True, "距趴下 2s 后起立放行"

    # 11. 保护期内 20Hz 重试保持拦截、到点放行(旧 Dog 的"同一 episode 只记一次"限流
    #     记账已随旧路径退役: active 下 FSM 静默返回 False, 无 journal 事件可断言)
    stub = StubLink()
    d = bridge.FsmActive(stub, now=0.0)
    for i in range(20):
        assert d.fsm.set_mode(2, now=2.0 + i * 0.05)[0] is False
    assert not names(stub), "被拦期间不得发任何指令"
    assert d.fsm.set_mode(2, now=bridge.config.STAND_GUARD_S)[0] is True

    # 12. (退役) 拦截 episode 的外部撤销(note_cancel/req_cancel 事件)是旧 Dog 专有的
    #     P1 日志机制; active 下无 episode 状态, C 回对面电平的请求撤销由 FSM frame() 的
    #     stand_req/lie_req 清除语义承担(test_fsm_table 覆盖)

    # 13. P0-3: submit 入队失败 -> set_mode 返回 False, 模式不推进、互锁锚点不动、
    #     镜像与真身一致(不得显示未发出的模式)
    class DeadLink:
        def __init__(self):
            self.cmds = []

        def submit(self, name, **p):
            self.cmds.append(name)
            return False

    dead = DeadLink()
    d = bridge.FsmActive(dead, now=0.0)
    bridge.state.mode = d.fsm.mode   # 镜像对齐真身(趴着): 隔离前序用例留下的陈旧镜像
    state_before = bridge.state.mode
    assert d.set_mode(2) is False, "入队失败必须返回 False(请求标志留给下一帧)"
    assert d.fsm.mode == 0 and d.fsm.rising is False
    assert d.fsm._last_big is None, "未真正发出的大动作不得刷新互锁锚点"
    assert bridge.state.mode == d.fsm.mode == state_before, \
        "入队失败不得把模式推进(state 镜像须与 FSM 真身一致)"

    print("test_safety: 12 组断言全部通过(无锚点互锁+note_cancel 记账 2 项随旧路径退役)")


if __name__ == "__main__":
    main()
