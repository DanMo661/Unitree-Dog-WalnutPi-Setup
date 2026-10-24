# -*- coding: utf-8 -*-
"""test_queue.py —— 指令队列(P0-3)单元测试, 无需硬件/狗库

覆盖:
  1. Move 合批: 批次里只保留最后一条, 且留在它原本的位置(不挪到队尾)
  2. TTL: Move 1.0s / 非 Move 3.0s 过期丢弃并计数
  3. 非 Move 串行 worker: 同一时刻最多一条在途; 慢请求不堵 Move 流
  4. 发送时刻 MODE_DWELL_S 兜底: StandUp 太近被丢(趴下/补平衡永不拦)
  5. submit: 链路未 up 返回 False 并计 drop_link
  6. 收权即时路径: worker 卡在 StandUp 时 StopMove/StandDown 100ms 内发出,
     不受 TTL, 且撤销队列里迟到的 StandUp/BalanceStand

用法: python run_test_shim.py test_queue.py
"""
import asyncio
import json
import time

import bridge

NAMES = {"StandUp": 1001, "StandDown": 1002, "BalanceStand": 1003,
         "StopMove": 1004, "Move": 1005, "Hello": 1006}


class FakePubSub:
    """记录下发指令; 非 Move 按 delay 模拟等回包, 并统计并发在途数"""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.sent = []            # [(name, payload)]
        self.inflight = 0
        self.max_inflight = 0

    def publish_without_callback(self, topic, payload):
        self.sent.append(("Move", payload))

    async def publish_request_new(self, topic, payload):
        api = payload.get("api_id")
        name = {v: k for k, v in NAMES.items()}.get(api, str(api))
        # 指令"发出"在调用瞬间完成, delay 只模拟等回包(收权路径要按发出时刻断言)
        self.sent.append((name, payload))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(self.delay)
            return {"data": {"header": {"status": {"code": 0}}, "data": None}}
        finally:
            self.inflight -= 1


class FakeConn:
    def __init__(self, ps):
        self.datachannel = type("DC", (), {"pub_sub": ps})()


def make_link(delay=0.0):
    link = bridge.DogLink.__new__(bridge.DogLink)   # 不走 __init__(不开线程)
    link.loop = asyncio.get_running_loop()
    link.q = asyncio.Queue()
    link._nm_q = asyncio.Queue()
    ps = FakePubSub(delay)
    link.conn = FakeConn(ps)
    link.drop_link = 0
    link.drop_ttl = 0
    link._drop_log_t = 0.0
    link._urgent_tasks = set()
    link._cancel_t = 0.0
    link._last_logged = None
    link._last_gear_read = None
    link._mv_n = 0
    return link, ps


def names(ps):
    out = []
    for n, p in ps.sent:
        if n == "Move":
            out.append(("Move", json.loads(p["parameter"])["x"]))
        else:
            out.append((n, None))
    return out


def put(link, name, t_off=0.0, **p):
    link.q.put_nowait((name, p, time.monotonic() + t_off))


async def case_merge():
    link, ps = make_link()
    tasks = [asyncio.ensure_future(link._consume()),
             asyncio.ensure_future(link._nonmove_worker())]
    try:
        put(link, "StandUp")
        put(link, "Move", x=0.1)
        put(link, "Move", x=0.2)
        put(link, "BalanceStand")
        await asyncio.sleep(0.15)
        # Move 只发最后一条(0.2); 非 Move 保持原相对顺序进串行 worker
        assert names(ps) == [("Move", 0.2), ("StandUp", None),
                             ("BalanceStand", None)], names(ps)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    print("ok  Move 合批只留最后一条且顺序不重排")


async def case_ttl():
    link, ps = make_link()
    tasks = [asyncio.ensure_future(link._consume()),
             asyncio.ensure_future(link._nonmove_worker())]
    try:
        put(link, "BalanceStand", t_off=-4.0)     # 过期非 Move(>3.0s)
        put(link, "Move", t_off=-2.0, x=1.0)      # 过期 Move(>1.0s, 本批最后一条)
        await asyncio.sleep(0.1)
        assert names(ps) == [], names(ps)
        assert link.drop_ttl == 2, link.drop_ttl
        put(link, "Move", x=0.5)                  # 新鲜, 应发出
        await asyncio.sleep(0.1)
        assert names(ps) == [("Move", 0.5)], names(ps)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    print("ok  TTL 过期丢弃并计数(Move 1s / 非 Move 3s)")


async def case_serial_and_move_priority():
    link, ps = make_link(delay=0.2)
    tasks = [asyncio.ensure_future(link._consume()),
             asyncio.ensure_future(link._nonmove_worker())]
    try:
        put(link, "Hello")
        put(link, "BalanceStand")
        put(link, "Move", x=0.7)
        await asyncio.sleep(0.08)                 # 第一条非 Move 还在等回包
        assert ("Move", 0.7) in names(ps), \
            "Move 被非 Move 的慢回包堵住(P0-3): %r" % names(ps)
        assert ps.inflight == 1, ps.inflight
        await asyncio.sleep(0.5)
        assert ps.max_inflight == 1, "同一时刻只允许一条非 Move 在途: %r" % ps.max_inflight
        assert names(ps)[-2:] == [("Hello", None), ("BalanceStand", None)], names(ps)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    print("ok  非 Move 串行且不堵 Move 流, 在途数恒为 1")


async def case_dwell():
    link, ps = make_link()
    tasks = [asyncio.ensure_future(link._consume()),
             asyncio.ensure_future(link._nonmove_worker())]
    try:
        bridge.state.last_big_sent = time.monotonic() - 0.5
        put(link, "StandUp")
        await asyncio.sleep(0.1)
        assert names(ps) == [], "发送时刻距上次大动作 < MODE_DWELL_S, StandUp 应丢"
        assert link.drop_ttl == 1
        put(link, "StandDown")                    # 趴下永远不拦(收权优先)
        await asyncio.sleep(0.1)
        assert names(ps) == [("StandDown", None)], names(ps)
        bridge.state.last_big_sent = time.monotonic() - 3.0
        put(link, "StandUp")
        await asyncio.sleep(0.1)
        assert ("StandUp", None) in names(ps), names(ps)
    finally:
        bridge.state.last_big_sent = None
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    print("ok  发送时刻互锁只拦 StandUp, 不拦趴下")


def case_submit_down():
    link = bridge.DogLink.__new__(bridge.DogLink)
    link.loop = None
    link.q = None
    link.drop_link = 0
    old = bridge.state.link
    bridge.state.link = "down"
    try:
        assert link.submit("Move", x=1.0) is False
        assert link.drop_link == 1, link.drop_link
    finally:
        bridge.state.link = old
    print("ok  submit 链路未 up 返回 False 并计 drop_link")


async def case_urgent_lock():
    """worker 卡在 StandUp 等回包时, 上锁(StopMove+StandDown)必须立即发出,
    不被 TTL 丢, 且撤销队列里尚未发出的 StandUp/BalanceStand"""
    link, ps = make_link(delay=0.6)
    tasks = [asyncio.ensure_future(link._consume()),
             asyncio.ensure_future(link._nonmove_worker())]
    old_link = bridge.state.link
    bridge.state.link = "up"
    try:
        put(link, "StandUp")
        await asyncio.sleep(0.05)              # worker 已取走, 正在等回包
        assert ps.inflight == 1, ps.inflight
        put(link, "StandUp")                   # 队列里待发: 上锁后必须撤销
        put(link, "BalanceStand")              # 同上
        d = bridge.Dog(link, now=0.0)
        d.mode = 1                             # 本地站立
        t0 = time.monotonic()
        assert d.set_mode(0, now=t0) is True
        while time.monotonic() - t0 < 0.1:
            ns = [n for n, _ in ps.sent]
            if "StopMove" in ns and "StandDown" in ns:
                break
            await asyncio.sleep(0.005)
        dt = time.monotonic() - t0
        ns = [n for n, _ in ps.sent]
        assert "StopMove" in ns and "StandDown" in ns, ns
        assert dt < 0.1, "收权指令被慢 worker 堵住: %.3fs" % dt
        await asyncio.sleep(0.7)               # 等在途 StandUp 回包
        ns = [n for n, _ in ps.sent]
        assert ns.count("StandUp") == 1, \
            "上锁后队列里迟到的 StandUp 未撤销: %r" % ns
        assert "BalanceStand" not in ns, \
            "上锁后迟到的 BalanceStand 未撤销: %r" % ns
    finally:
        bridge.state.link = old_link
        bridge.state.last_big_sent = None
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    print("ok  上锁/趴下即时发出, 不排慢 worker、不被 TTL 丢、撤销迟到起立指令")


async def _main_async():
    await case_merge()
    await case_ttl()
    await case_serial_and_move_priority()
    await case_dwell()
    await case_urgent_lock()


def main():
    bridge.RTC_TOPIC = {"SPORT_MOD": "lf/test"}
    bridge.SPORT_CMD_MCF = dict(NAMES)
    case_submit_down()
    asyncio.run(_main_async())
    print("test_queue: 6 组断言全部通过")


if __name__ == "__main__":
    main()
