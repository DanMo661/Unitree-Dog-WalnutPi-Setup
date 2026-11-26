# -*- coding: utf-8 -*-
"""重连看门狗逻辑仿真(无需硬件/狗库): 验证 _guarded_consume 的两种拆会话路径
用法: python test_reconnect_sim.py
"""
import asyncio

import bridge


async def main():
    dl = bridge.DogLink.__new__(bridge.DogLink)   # 不走 __init__(那会开线程/依赖狗库)
    dl.q = asyncio.Queue()

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


if __name__ == "__main__":
    asyncio.run(main())
