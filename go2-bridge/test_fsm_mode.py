# -*- coding: utf-8 -*-
"""test_fsm_mode.py —— FSM 执行开关(T3.4)三用例的仿真验证(shadow/active/webface 调用面契约)

验证目标: 同一段遥控时序下, 指令到底由哪条路径产生(下发来源), 且两路互斥:
  shadow  : 旧 rc_loop/Dog 真实下发; DogFsm 影子只比对(FSM 独有的指令不得出现在链路上)
  active  : DogFsm 真实下发; 旧 Dog/rc_loop 一次都不执行
做法: 给两条路径各埋一个对方不可能产生的哨兵 —— 旧路径 set_gear 发 LEGACY_MARK,
FSM set_gear 发 FSM_ONLY_MARK, 再看桩链路收到了谁。

两个模式各起一个子进程(各自一个 rc_loop 线程), 避免同一进程里两条 20Hz 循环互相踩。
用法: python run_test_shim.py test_fsm_mode.py   (或 python test_fsm_mode.py)
"""
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

IDX = {"A": 4, "B": 5, "C": 6, "G": 9, "P": 1, "R": 3, "Y": 0}

# 时序: 首连(无遥测回落 mode=1) -> A 翻拨解锁 -> C 下->上边沿进走模式 -> 同步挡位1
SCRIPT = [
    (0.0, {"A": 1002, "C": 282, "G": 282}),
    (0.3, {"A": 1722}),   # 解锁(首帧之后, 只认边沿)
    (0.5, {"C": 1722}),   # C 下->上: 站/走, 随后按挡位1 同步步态
]

FAILS = []


def ok(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


class StubLink:
    """下发桩: 只记录, 不出门"""

    def __init__(self):
        self.cmds = []

    def submit(self, name, **p):
        self.cmds.append((name, dict(p)))
        return True


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
        cur = {}
        for start, ev in SCRIPT:
            if t >= start:
                cur.update(ev)
        ch = [1002] * 16
        for k, v in cur.items():
            ch[IDX[k]] = v
        return ch, False, False


def wait_until(fn, timeout, what):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if fn():
            return True
        time.sleep(0.02)
    raise AssertionError("超时: " + what)


def names(stub):
    return [n for n, _p in stub.cmds]


def install_sentinels():
    """给两条路径各埋哨兵: 谁的哨兵到桩链路, 就是谁在下发"""
    import bridge
    import core.fsm

    orig_gear = bridge.Dog.set_gear

    def legacy_gear(self, g):
        self.link.submit("LEGACY_MARK")
        return orig_gear(self, g)

    def fsm_gear(self, g):
        return [("submit", "FSM_ONLY_MARK", {})]

    bridge.Dog.set_gear = legacy_gear
    core.fsm.DogFsm.set_gear = fsm_gear

    # 仿真缩短时间常数(两侧同用一套 config, 等价路径)
    bridge.config.STAND_GUARD_S = 0.05
    bridge.config.MODE_DWELL_S = 0.05
    bridge.config.STAND_TIMEOUT_S = 0.5


def run_shadow():
    import bridge
    print("== shadow: 旧路径执行 + FSM 影子只比不发 ==")
    bridge.config.FSM_MODE = "shadow"
    bridge.config.FSM_SHADOW = True
    bridge._SHADOW = bridge._ACTIVE = None
    install_sentinels()

    stub = StubLink()
    dog = bridge.Dog(stub, now=time.monotonic())
    bridge.state.link = "up"
    bridge.state.link_gen = 1
    dog.reset()                       # 模拟 DogLink 首连回调: 无遥测回落 mode=1
    ok(dog.mode == 1, "旧 Dog 首连回落站立")

    bridge.Sbus = FakeSbus
    threading.Thread(target=bridge.rc_loop, args=(dog, None), daemon=True).start()

    wait_until(lambda: bridge._SHADOW is not None, 2, "影子对象创建")
    ok(isinstance(bridge._SHADOW, bridge.FsmShadow), "shadow 模式建的是 FsmShadow")
    ok(bridge._ACTIVE is None, "shadow 模式无 FsmActive")
    wait_until(lambda: "LEGACY_MARK" in names(stub), 5, "旧路径真实下发")
    wait_until(lambda: bridge._SHADOW.fsm.gear_sent == 1, 2, "影子吃到同样事件")
    time.sleep(0.2)

    got = names(stub)
    ok("FSM_ONLY_MARK" not in got, "影子 FSM 的指令没有下发(只比不发)")
    ok(got.count("LEGACY_MARK") == 1, "旧路径只下发一次(无双路重复)")
    ok("FreeWalk" in got, "旧路径按挡位1 真实发步态(10-21 起挡1=FreeWalk)")
    ok(bridge.state.mode == 2, "state.mode 镜像到走模式")
    ok(bridge.state.rc_armed is True, "state.rc_armed 镜像已解锁")
    ok(bridge._SHADOW.fsm.mode == 2, "影子 FSM 同步到走模式")


def run_active():
    import bridge
    print("== active: FSM 执行, 旧路径停用 ==")
    bridge.config.FSM_MODE = "active"
    bridge.config.FSM_SHADOW = True   # 故意开着: active 必须压过影子开关, 不建影子
    bridge._SHADOW = bridge._ACTIVE = None
    install_sentinels()

    stub = StubLink()
    dog, sh = bridge._make_controller(stub, time.monotonic())
    ok(isinstance(dog, bridge.FsmActive), "active 模式构造的是 FsmActive")
    ok(sh is None, "active 模式不建影子(与 FSM_SHADOW=True 互斥)")
    bridge._ACTIVE = dog
    bridge.state.link = "up"
    bridge.state.link_gen = 1
    dog.reset()                       # 模拟 DogLink 首连回调: 无遥测回落 mode=1
    ok(bridge.state.mode == 1, "FSM 首连回落站立")

    bridge.Sbus = FakeSbus
    threading.Thread(target=bridge.rc_loop, args=(dog, None), daemon=True).start()

    wait_until(lambda: "FSM_ONLY_MARK" in names(stub), 5, "FSM 输出落地")
    wait_until(lambda: dog.fsm.gear_sent == 1, 2, "FSM 挡位已同步")
    time.sleep(0.2)

    got = names(stub)
    ok("LEGACY_MARK" not in got, "旧 Dog 路径未执行(旧路径已停用)")
    ok(got.count("FSM_ONLY_MARK") == 1, "FSM 指令只下发一次(无双路重复)")
    ok(bridge._SHADOW is None, "active 模式没有影子对象")
    ok(bridge.state.mode == 2, "state.mode 镜像到走模式")
    ok(bridge.state.rc_armed is True, "state.rc_armed 镜像已解锁")
    ok(bridge.state.relay_us == 1050 and bridge.state.relay_on is False,
       "FSM 继电器安全位落地(state 镜像)")
    ok(bridge._SHADOW is None and bridge._ACTIVE is dog, "唯一执行句柄是 FsmActive")


def run_web_interface():
    """web_server 调用面契约(nitpick 10-31 #1/#10): active 下 d 必须实现 ws 侧调用的
    全部方法; 模拟 ws finally 序列(hold + closed)不得抛异常 —— #1 的 FsmActive 缺
    hold 正漏在这条缝里(关网页钩子半途夭折, FSM 收不到 closed, 残留杆量被 tick 持续下发)。"""
    import bridge
    print("== active: web_server 调用面契约 ==")
    bridge.config.FSM_MODE = "active"
    bridge._SHADOW = bridge._ACTIVE = None
    dog, _ = bridge._make_controller(StubLink(), time.monotonic())
    for name in ("hold", "set_mode", "web_cmd", "trick", "rising", "rise_t",
                 "reset", "link_poll", "frame", "tick", "safety",
                 "serial_reopened", "serial_offline"):
        ok(hasattr(dog, name), "FsmActive 实现 web 侧调用的 %s()" % name)
    bridge.state.link = "up"
    dog.reset()
    dog.fsm.link_up = True            # web_cmd("closed") 的停走分支直接依赖它
    dog.web_cmd("arm")
    ok(dog.fsm.web_armed is True, "网页 arm 生效(可控)")
    dog.hold()                        # ws finally 第一连击: #1 在此抛 AttributeError
    dog.web_cmd("closed")             # ws finally 第二连击
    ok(dog.fsm.web_armed is False, "closed 真正送达 FSM, 网页控制解除")
    ok(dog.fsm.moving is False, "hold 幂等停走落地")


def run_case(case):
    if case == "shadow":
        run_shadow()
    elif case == "active":
        run_active()
    elif case == "webface":
        run_web_interface()
    else:
        raise SystemExit("未知用例: %r" % (case,))
    if FAILS:
        print("FAIL:", ", ".join(FAILS))
        raise SystemExit(1)
    print("用例通过:", case)


def check_switch_helper():
    """_fsm_mode 的取值/回落(默认 active, 10-31 shadow 零差异过门后翻转;
    旧名 live/拼错一律回落旧路径=安全方向)"""
    import bridge
    print("== FSM_MODE 取值与回落 ==")
    ok(bridge.config.FSM_MODE == "active", "仓库默认 FSM_MODE=active(10-31 翻转)")
    ok(bridge._fsm_mode() == "active", "默认解析为 active")
    bridge.config.FSM_MODE = "ACTIVE"
    ok(bridge._fsm_mode() == "active", "大小写不敏感")
    bridge.config.FSM_MODE = "live"
    ok(bridge._fsm_mode() == "shadow", "旧名 live 回落 shadow(失败方向=旧路径)")
    bridge.config.FSM_MODE = "shadow"
    ok(bridge._fsm_mode() == "shadow", "shadow 原样识别")
    bridge.config.FSM_MODE = "active"


def main():
    case = os.environ.get("FSM_MODE_CASE")
    if case:
        run_case(case)
        return
    check_switch_helper()
    rc_all = 0
    for c in ("shadow", "active", "webface"):
        env = dict(os.environ, FSM_MODE_CASE=c)
        r = subprocess.run([sys.executable,
                            os.path.join(HERE, "run_test_shim.py"),
                            os.path.join(HERE, "test_fsm_mode.py")],
                           cwd=HERE, env=env)
        if r.returncode != 0:
            rc_all = 1
    if FAILS or rc_all:
        print("test_fsm_mode: FAIL")
        raise SystemExit(1)
    print("test_fsm_mode: 三用例(shadow/active/webface)全部通过")


if __name__ == "__main__":
    main()
