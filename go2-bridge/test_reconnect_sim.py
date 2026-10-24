# -*- coding: utf-8 -*-
"""重连仿真(无需硬件/狗库):
  1) _guarded_consume 的两种拆会话路径(看门狗/接收循环退出)
  2) P0-4 重连对账: 每次 reset 用新鲜高度 + 滞回对账本地 mode, 不对已站立的狗
     发 StandUp; 中间区不臆测; 未完成的起立流程取消且不补 BalanceStand
用法: python test_reconnect_sim.py
"""
import asyncio
import time

import bridge


class StubLink:
    """Dog 发送桩: 只记录是否入队成功"""

    def __init__(self):
        self.cmds = []

    def submit(self, name, **p):
        self.cmds.append((name, dict(p)))
        return True


def names(stub):
    return [n for n, _ in stub.cmds]


def fresh(height, ts=None):
    bridge.state.dog_height = height
    bridge.state.dog_state_t = time.monotonic() if ts is None else ts


def reconcile_cases():
    """P0-4: reset() 对账(每次重连都做, 不是只有首连)"""
    state = bridge.state

    # 1) 本地 mode=2(陈旧)但狗重连后趴着(h=0.10) -> 对账回 0
    stub = StubLink()
    d = bridge.Dog(stub, now=0.0)
    d.mode = 2
    state.link_gen = 2
    fresh(0.10)
    d.reset()
    assert d.mode == 0, "重连后狗趴着, 本地 mode 必须对账回 0(否则摇杆发给趴狗)"
    assert names(stub) == ["StopMove"], names(stub)
    assert state.mode == 0

    # 2) 本地 mode=0 但狗站着(h=0.30) -> 对账到 1, 且不补 StandUp;
    #    C 上位补同步 1->2 也只发 StopMove, 绝不对已站立的狗发 StandUp
    stub = StubLink()
    d = bridge.Dog(stub, now=0.0)
    d.mode = 0
    state.link_gen = 3
    fresh(0.30)
    d.reset()
    assert d.mode == 1, "重连后狗站着, 本地 mode 必须对账为 1"
    assert "StandUp" not in names(stub), "重连不得对已站立的狗发 StandUp"
    assert d.set_mode(2, now=time.monotonic()) is True
    assert names(stub) == ["StopMove", "StopMove"], names(stub)
    assert "StandUp" not in names(stub) and "BalanceStand" not in names(stub)

    # 3) 滞回中间区(h=0.20)不臆测: mode 保持, 等下一包; 下一包 0.32 -> 1
    stub = StubLink()
    d = bridge.Dog(stub, now=0.0)
    d.mode = 2
    state.link_gen = 4
    fresh(0.20)
    d.reset()
    assert d.mode == 2, "中间区不得臆测改写本地 mode"
    assert d._sync_pending is True, "中间区应保留对账未决, 等下一包"
    fresh(0.32)
    d.tick()
    assert d.mode == 1, "下一包高度决定性后才对账"
    assert d._sync_pending is False

    # 4) reset 取消未完成的起立(rising)且不补 BalanceStand
    stub = StubLink()
    d = bridge.Dog(stub, now=0.0)
    d.mode = 2
    d.rising = True
    d.rise_t = time.monotonic() - 1.0
    d.rise_deadline = time.monotonic() + 5.0
    state.link_gen = 5
    fresh(0.30)
    d.reset()
    assert d.rising is False, "重连必须取消未完成的起立流程"
    assert "BalanceStand" not in names(stub), "重连不得补发 BalanceStand(会把半起的狗弹起)"

    # 5) 无有效高度: 重连保持原 mode, 对账挂起等遥测(不假定, 不发 StandUp)
    stub = StubLink()
    d = bridge.Dog(stub, now=0.0)
    d.mode = 2
    state.link_gen = 6
    bridge.state.dog_height = None
    bridge.state.dog_state_t = None
    d.reset()
    assert d.mode == 2 and d._sync_pending is True
    assert "StandUp" not in names(stub)

    # 6) 有效高度范围校验: 超出 0.05~0.5 的离谱值不采信
    fresh(0.30)
    assert bridge._dog_height_valid()
    assert not bridge._dog_height_valid(time.monotonic() + 5.0), "陈旧遥测不得采信"
    fresh(0.90)
    assert not bridge._dog_height_valid(), "超范围高度不得采信"
    fresh(0.30)
    assert bridge._dog_height_valid()

    print("重连对账 6 组断言通过")


async def wait_state_cases():
    """P0-4: _wait_dog_state 只认有效高度(非 None 且在合理范围)"""
    old_wait = bridge.config.DOG_STATE_WAIT_S
    bridge.config.DOG_STATE_WAIT_S = 0.3
    try:
        dl = bridge.DogLink.__new__(bridge.DogLink)
        bridge.state.dog_height = None
        bridge.state.dog_state_t = time.monotonic()   # 有包时间戳但高度 None
        t0 = time.monotonic()
        ok = await dl._wait_dog_state()
        assert ok is False and time.monotonic() - t0 >= 0.25, "高度无效必须等到超时"
        bridge.state.dog_height = 0.30
        bridge.state.dog_state_t = time.monotonic()
        t0 = time.monotonic()
        ok = await dl._wait_dog_state()
        assert ok is True and time.monotonic() - t0 < 0.2, "有效高度应立即返回"
    finally:
        bridge.config.DOG_STATE_WAIT_S = old_wait
    print("等待有效狗姿态 2 组断言通过")


async def main():
    dl = bridge.DogLink.__new__(bridge.DogLink)   # 不走 __init__(那会开线程/依赖狗库)
    dl.q = asyncio.Queue()
    dl._nm_q = asyncio.Queue()

    # 路径1: 看门狗先判死(网线拔出) -> 会话必须带"看门狗"原因收场
    async def consume():
        await asyncio.sleep(60)                   # 模拟静默阻塞, lib 不报错
    async def watch():
        await asyncio.sleep(0.2)
        return                                    # 返回即判死
    dl._consume, dl._watch_link = consume, watch
    try:
        await dl._guarded_consume()
        raise AssertionError("看门狗判死未拆会话")
    except RuntimeError as e:
        assert "看门狗" in str(e), e

    # 路径2: 接收循环先异常退出(ws 断了) -> 会话带原始异常收场
    async def consume2():
        await asyncio.sleep(0.1)
        raise ValueError("websocket died")
    async def watch2():
        await asyncio.sleep(60)
    dl._consume, dl._watch_link = consume2, watch2
    try:
        await dl._guarded_consume()
        raise AssertionError("接收循环退出未拆会话")
    except RuntimeError as e:
        assert "接收循环退出" in str(e) and "websocket died" in str(e), e

    print("看门狗两条拆会话路径 OK")

    await wait_state_cases()


if __name__ == "__main__":
    reconcile_cases()
    asyncio.run(main())
