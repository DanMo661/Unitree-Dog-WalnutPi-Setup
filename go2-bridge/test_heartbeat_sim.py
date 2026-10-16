# -*- coding: utf-8 -*-
"""心跳闭环仿真(无需硬件): 狗正常回包=心跳永续; 狗应用层装死(会话半死)=连续 miss 后退出触发重连
用法: python run_test_shim.py test_heartbeat_sim.py   (不碰真实串口, 不向狗发指令)
"""
import asyncio
import time

import bridge


class FakePubSub:
    def __init__(self, delay):
        self.delay = delay          # None=永不回包(模拟会话半死: 信令活着/指令黑洞)

    async def publish_request_new(self, topic, payload):
        if self.delay is None:
            await asyncio.Future()  # 永不完成
        await asyncio.sleep(self.delay)
        return {"data": {"header": {"status": {"code": 0}}, "data": 1}}


class FakeConn:
    def __init__(self, ps):
        self.datachannel = type("DC", (), {"pub_sub": ps})()


def fresh_link(delay):
    bridge.config.HEARTBEAT_S = 0.05       # 仿真提速(真机 5s), 代码路径相同
    bridge.config.HEARTBEAT_TIMEOUT = 0.1  # 真机 4s
    bridge.config.HEARTBEAT_MISS = 2
    # 本地没装 unitree_webrtc_connect 时这两个常量不存在(板上由真库导入), 测试注入桩值
    bridge.SPORT_CMD_MCF = {"GetSpeedLevel": 1026}
    bridge.RTC_TOPIC = {"SPORT_MOD": "lf/test"}
    link = bridge.DogLink()
    link.conn = FakeConn(FakePubSub(delay))
    return link


def main():
    # 1. 狗正常回包: 心跳跑 8 个周期绝不退出
    link = fresh_link(delay=0.01)

    async def alive():
        task = asyncio.ensure_future(link._heartbeat())
        await asyncio.sleep(0.4)
        assert not task.done(), "狗正常回包时心跳退出(误报半死)"
        task.cancel()

    asyncio.run(alive())

    # 2. 会话半死(永不回包): 2 次 miss 内退出(触发拆会话重连)
    link2 = fresh_link(delay=None)
    t0 = time.monotonic()
    asyncio.run(asyncio.wait_for(link2._heartbeat(), 3))
    dt = time.monotonic() - t0
    assert dt < 1.5, "半死检测太慢: %.2fs" % dt
    print("心跳闭环断言通过: 存活路径不退出 / 半死路径 %.2fs 内判死拆会话" % dt)


if __name__ == "__main__":
    main()
