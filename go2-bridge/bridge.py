# -*- coding: utf-8 -*-
"""Go2 SBUS 遥控桥
云卓 G12 SBUS -> 板子解码 -> WebRTC(App 同款通道, MCF 指令) -> 宇树 Go2
浏览器遥控页: http://<板子IP>:<WEB_PORT>  (RC 解锁时 RC 优先, 网页仅 RC 上锁时生效)

设计: 三个线程各管各的, 互不阻塞、各自自愈 ——
  DogLink 线程: WebRTC 会话, 断线/拔网线自动重连(载波+信令口 TCP 看门狗强拆死会话)
  rc_loop 线程: SBUS 串口, 未接/拔出每 3s 重试, 串口重开需重新解锁
  web 线程:     FastAPI + WebSocket, 浏览器关闭即解除网页控制并停走保持姿态(ADR-0003, 不趴)
板子可以先于狗/接收机上电, 什么都不接也正常启动。
"""
import math
import signal
import socket
import threading
import time
import os

import config
import journal
from sbus import Sbus
from servo import Servo, GpioOut, RelayPwm
from pa import Pa          # P0-1/T2.2 后 main 不再用 Pa, 但测试经 bridge.Pa 构造, 保留导出
import netrc
from stats import SbusStats, sbus_stats   # 帧统计拆分(行为零变更): 两名字原样可用
from telemetry import (State, state, log, _LOG_BUF, _J_EVERY_N, _T0,
                       _ev, _EVENTS, _set_cmd_result, _dog2_snapshot,
                       _dog_height_valid, clamp01)   # 遥测/日志拆分: 名字原样可用, 同一对象
from link import (DogLink, DOG_LIB, _path_ok, _pick_robot_ip,
                  _PROBE_SEEN, PROBE_TOPICS)   # WebRTC 链路拆分: 名字原样可用
from webserver import web_server, _sys_snapshot, _ver, _audio_state   # 网页服务拆分: 名字原样可用
try:   # 狗库类/常量: 装了狗库才在 link(与拆分前的 bridge)命名空间存在, 未装则保持不存在
    from link import (UnitreeWebRTCConnection, WebRTCConnectionMethod,
                      RTC_TOPIC, SPORT_CMD_MCF)
except ImportError:
    pass


def _ctl_snapshot(now):
    """控制抑制快照(P1): /state 与 /ws 共用, 只读 dog/link 字段, 不持锁不写。"""
    ctl = {"blocked": None, "remain": 0.0, "target": None,
           "rising_age": None, "rise_deadline": None,
           "q_depth": 0, "drop_link": 0, "drop_ttl": 0,
           "last_big_age": (round(now - state.last_big_sent, 2)
                            if state.last_big_sent is not None else None)}
    d = dog
    if d is None:
        return ctl
    ctl["blocked"] = d._blk_reason
    ctl["target"] = d._blk_target
    if d._blk_reason is not None:
        ctl["remain"] = round(max(0.0, d._blk_until - now), 2)
    if d.rising:
        ctl["rising_age"] = round(now - d.rise_t, 2)
        ctl["rise_deadline"] = round(max(0.0, d.rise_deadline - now), 2)  # 距兜底剩余秒
    try:
        st = d.link.stats()
        ctl["q_depth"] = st.get("q_depth", 0)
        ctl["drop_link"] = st.get("drop_link", 0)
        ctl["drop_ttl"] = st.get("drop_ttl", 0)
    except AttributeError:
        pass                      # 测试桩无 stats(): 保持零值
    return ctl


def axis(raw, dz=0.06):
    """通道原始值 282~1722 -> -1..1, 中值/半行程见 config.AXIS_MID/AXIS_SPAN (G12 实测), 带死区"""
    v = max(-1.0, min(1.0, (raw - config.AXIS_MID) / float(config.AXIS_SPAN)))
    if abs(v) < dz:
        return 0.0
    return (v - math.copysign(dz, v)) / (1 - dz)


def pos(raw, n=3):
    """n 段开关原始值 -> 0..n-1"""
    return min(n - 1, max(0, round((raw - config.POS_MIN) / float(config.POS_MAX - config.POS_MIN) * (n - 1))))


dog = None             # main() 里赋值; DogLink 连上狗后回调其 reset()


_ACTIVE = None        # FsmActive 单例(main() 赋值; webserver._fsm_feed 读取)


class FsmActive:
    """FSM 执行器(T3.4, ADR-0004 后唯一控制器): DogFsm 是唯一指令源。

    事件喂法(rc_loop/DogLink/web 各线程按原时序喂入, 同一把锁)。输出落地分两路:
      submit 类 —— 不在输出列表里: 构造时注入 send=link.submit, FSM 在事件内
                   同步真发(指令日志/journal 由 DogLink.submit 自带), 并按返回值
                   决定是否提交本地状态(入队失败不提交, 与旧 Dog 同门);
      其余输出 —— _exec 落地: ("relay", us) -> Servo.set_us(us);
                   ("gpio", level) -> GpioOut.set_level(level); ("log", msg) -> log(msg)
    互斥保证: 旧 Dog/FsmShadow 双轨已删(ADR-0004), main 只构造本类, rc_loop 走
    _rc_loop_active —— 同一时刻只有这一条路径能向下发指令。"""

    active = True

    def __init__(self, link, now=None):
        from core.fsm import DogFsm   # 惰性导入: core/ 未部署则启动即失败(部署清单必须带 core/)
        self.link = link
        self.lock = threading.Lock()
        # submit 直接注入落地函数(阶段3 HIGH 修复): FSM 在事件内同步真发并按
        # 返回值决定是否提交本地状态 —— 与旧 Dog.set_mode/_begin_rise 的
        # "入队失败不提交"门完全同构, out 里不再有 submit(不会二次发送)。
        self.fsm = DogFsm(now=now, send=self.link.submit)
        self._sv1 = None
        self._svs = []
        self._warned_raw_submit = False   # _exec 兜底分支只告警一次
        # /state ctl 快照字段: 拦截 episode 是旧路径专有, FSM 不维护, 恒空
        self._blk_reason = None
        self._blk_target = None
        self._blk_until = 0.0
        self._last_link = state.link
        self._prev = self._snap()

    # ---- 输出落地 ----

    def bind_io(self, sv1, svs):
        """rc_loop 建好 PWM/GPIO 后注入(active 模式下继电器只由 FSM 命令驱动)"""
        self._sv1, self._svs = sv1, list(svs)

    def _exec(self, cmds):
        for c in cmds:
            kind = c[0]
            if kind == "submit":
                # 正常不会走到: send 注入后 FSM 已在事件内同步真发, out 只剩
                # relay/gpio/log。留作接线回归保险 —— 真发并大声提示一次,
                # 绝不静默丢指令(send 语义的失败回传只在 FSM 事件内部成立)。
                if not self._warned_raw_submit:
                    self._warned_raw_submit = True
                    log("FSM active: 出现未经 send 落地的 submit(接线异常), 已直发:", c[1], lvl="warn")
                self.link.submit(c[1], **c[2])
            elif kind == "relay":
                us = c[1]
                was_us = self._sv1.cur_us if self._sv1 is not None else None
                was_lv = [s.level for s in self._svs]
                if self._sv1 is not None:
                    self._sv1.set_us(us)
                state.relay_us = us
                state.relay_on = us >= 1650     # config 注释的继电器分界(1950 吸合/1050 断)
                if self._sv1 is not None:
                    _relay_journal(self._sv1, self._svs, was_us, was_lv)
            elif kind == "gpio":
                lv = bool(c[1])
                was_us = self._sv1.cur_us if self._sv1 is not None else None
                was_lv = [s.level for s in self._svs]
                for s in self._svs:
                    s.set_level(lv)
                state.relay_on = lv
                if self._sv1 is not None:
                    _relay_journal(self._sv1, self._svs, was_us, was_lv)
            elif kind == "log":
                log(c[1], lvl="debug")

    # ---- FSM 真身 -> state 镜像(/state 与网页端口径不变) ----

    def _snap(self):
        f = self.fsm
        return (f.armed, f.mode, f.gear_sent, f.action)

    def _sync(self):
        f = self.fsm
        prev_armed, prev_mode, prev_gear, prev_action = self._prev
        state.mode = f.mode
        state.rc_armed = f.armed
        state.web_armed = f.web_armed
        state.gear = f.gear
        state.action = f.action
        state.action_end = f.action_end
        state.sbus_ok = f.sbus_ok
        state.sbus_clean = f.sbus_clean
        if f.last_arm_edge is not None:
            state.arm_blocked_until = f.last_arm_edge + config.ARM_DEBOUNCE_S
        # 黑匣子事件: 旧路径在 Dog 里记, active 下由 FSM 状态变化等价补记
        if f.armed != prev_armed:
            journal.record("event", ev="arm" if f.armed else "disarm", src="fsm")
            _ev("rc", "arm" if f.armed else "disarm")
        if f.mode != prev_mode:
            journal.record("event", ev="mode", prev=prev_mode, to=f.mode)
            _ev("mode", "mode", "%s→%s" % (prev_mode, f.mode))
        if f.gear_sent != prev_gear:
            journal.record("event", ev="gear", g=f.gear_sent)
        if f.action != prev_action and f.action is not None:
            journal.record("event", ev="trick", name=f.action)
        self._prev = self._snap()

    # ---- /ctl 与网页端读取的兼容属性 ----

    @property
    def rising(self):
        return self.fsm.rising

    @property
    def rise_t(self):
        return self.fsm.rise_t

    @property
    def rise_deadline(self):
        return self.fsm.rise_deadline

    def last_clean_gap(self, now):
        """失控保护计时: 与旧 rc_loop 的 now-last_clean 同口径"""
        return now - self.fsm.last_clean

    # ---- 跨线程事件(DogLink / 网页 / TRICK) ----

    def reset(self):
        """DogLink (重)连回调: 与旧 dog.reset() 同刻, 喂 FSM link 事件并落地输出"""
        with self.lock:
            self._last_link = state.link
            self.fsm.set_link(state.link == "up")
            self._exec(self.fsm.link(state.link_gen, height=state.dog_height,
                                     height_t=state.dog_state_t))
            self._sync()

    def link_poll(self):
        with self.lock:
            if state.link_gen != self.fsm.link_gen_cur:
                self._exec(self.fsm.link(state.link_gen, height=state.dog_height,
                                         height_t=state.dog_state_t))
            if state.link != self._last_link:
                self._last_link = state.link
                self.fsm.set_link(state.link == "up")
            self._sync()

    def web_cmd(self, cmd, vx=None, vy=None, vyaw=None):
        with self.lock:
            self._exec(self.fsm.web_cmd(cmd, vx=vx, vy=vy, vyaw=vyaw))
            self._sync()

    def hold(self):
        """失控保护/网页断开停走(web_server finally 直接调用, nitpick#1):
        FSM 幂等 StopMove —— 缺此方法会让关网页钩子抛 AttributeError 半途夭折,
        FSM 收不到 closed, 网页残留杆量被 tick 持续下发(跑飞风险)"""
        with self.lock:
            self._exec(self.fsm.hold())
            self._sync()

    def set_mode(self, m):
        """网页端入口(web_server 直接调用 d.set_mode): 执行 FSM 输出并同步镜像"""
        with self.lock:
            ok, cmds = self.fsm.set_mode(m)
            self._exec(cmds)
            self._sync()
        return ok

    def trick(self, name, dur):
        with self.lock:
            self.fsm.set_link(state.link == "up")
            cmds, result = self.fsm.trick(name, dur)
            self._exec(cmds)
            self._sync()
        return result == "OK"

    # ---- rc_loop 线程事件: 输出在本函数内落地 ----

    def frame(self, ch, fs, lost, source, now):
        with self.lock:
            self._exec(self.fsm.frame(ch, fs, lost, source=source, now=now,
                                      height=state.dog_height,
                                      height_t=state.dog_state_t))
            self._sync()

    def tick(self, now):
        with self.lock:
            self._exec(self.fsm.tick(now=now, height=state.dog_height,
                                     height_t=state.dog_state_t))
            self._sync()

    def safety(self, gap_clean, now):
        with self.lock:
            self._exec(self.fsm.safety(gap_clean, now=now))
            self._sync()

    def serial_reopened(self):
        with self.lock:
            self._exec(self.fsm.serial_reopened())
            self._sync()

    def serial_offline(self):
        with self.lock:
            self._exec(self.fsm.serial_offline())
            self._sync()


def _make_controller(link, now):
    """构造唯一控制器(ADR-0004): 旧 Dog/FsmShadow 双轨已删, FSM active 是唯一执行路径。
    core/ 未部署时 FsmActive 构造抛 ImportError 启动即失败 —— 部署清单必须带 core/。"""
    return FsmActive(link, now=now)


def _relay_journal(sv1, svs, was_us, was_lv):
    """journal 埋点(kind=relay): 只记实际写盘成功的 PWM 目标脉宽 / GPIO 电平翻转。
    was_us/was_lv 为写入前快照(sv1.cur_us / [s.level]); servo/GpioOut 内部只在
    写 sysfs 成功后才更新这两个属性, 故"现值 != 快照"即等价于"本次真实写过"。"""
    if sv1.cur_us is not None and sv1.cur_us != was_us:
        journal.record("relay", out="pwm1", us=round(sv1.cur_us))
    for s, was in zip(svs, was_lv):
        if s.level != was:
            journal.record("relay", out="gpio", gpio=s.gpio_num, level=bool(s.level))


_LOOP_STALL_LOG_T = 0.0


def _loop_alive_check(now):
    """rc_loop(独立线程)检测桥事件循环是否停摆: 心跳/看门狗/指令下发全跑在循环里,
    循环一卡它们集体失明(横幅还显示可控)。停摆 ≥10s 报线, 持续停摆期间每 10s 一条。
    loop_ts 为 None = 循环还没起(首次连接前), 不报。"""
    global _LOOP_STALL_LOG_T
    ts = state.loop_ts
    if ts is not None and now - ts >= 10.0 and now - _LOOP_STALL_LOG_T >= 10.0:
        _LOOP_STALL_LOG_T = now
        log("桥事件循环停摆 %.0fs(板子 IO 卡死?), 控制指令可能没有发出!" % (now - ts), lvl="error")


def _rc_loop_active(d):
    """现役主循环(T3.4): 串口/netrc/节拍/journal 驱动, 控制决策与指令全部来自 DogFsm
    (set_mode/move/trick 等经由 FSM 事件内同步落地), 全进程唯一 submit/set_us/set_level 出口。"""
    sbus = Sbus(config.SBUS_PORT, on_frames=sbus_stats.feed)
    svs = [GpioOut(g, log) for g in config.RELAY_GPIOS]   # 38/40/37/35脚: IO电平
    sv1 = RelayPwm([Servo(config.SERVO_CHIP, ch,          # B键继电器: PWM1+PWM2 双路同值并出
                          config.SERVO_MIN_US, config.SERVO_MAX_US, log)
                    for ch in (config.SERVO1_CH, config.SERVO2_CH)])
    d.bind_io(sv1, svs)
    jf_n = 0
    while True:
        _loop_alive_check(time.monotonic())
        if not sbus.opened:
            if sbus.open():
                log("SBUS 串口已打开:", config.SBUS_PORT)
                d.serial_reopened()          # 串口重开必须重新解锁(FSM 内部复位)
            else:
                d.serial_offline()
                time.sleep(3)
                continue
        t0 = time.monotonic()      # 节拍锚点: read 超时计入周期
        f = sbus.read()
        nf = netrc.get()
        if nf is not None:
            f = (nf, False, False)   # 网络遥控新鲜(300ms)则优先
        state.last_src = "netrc" if nf is not None else "sbus"
        now = time.monotonic()
        d.link_poll()                # (重)连/set_link 事件按 state 轮询补喂
        if f is not None:
            ch, fs, lost = f
            state.last_frame_t = now
            d.frame(ch, fs, lost, state.last_src, now)   # 含坏帧 hold / 干净帧全量控制
        d.tick(now)                  # 兜底 drain + 网页摇杆(干净帧下幂等)
        gap_clean = d.last_clean_gap(now)
        if f is not None:
            jf_n += 1
            if fs or lost or jf_n % _J_EVERY_N == 0:
                journal.record("frame", source=state.last_src, ch=ch,
                               fs=fs, lost=lost, gap_clean=round(gap_clean, 3))
        d.safety(gap_clean, now)     # 失控保护 + sbus_ok/clean 镜像
        time.sleep(max(0.0, 1.0 / config.SEND_HZ - (time.monotonic() - t0)))


def rc_loop(d, pa=None):
    # 旧 rc_loop/Dog 控制路径已删(ADR-0004), 委托现役循环; pa 形参仅留签名兼容(测试在用)
    return _rc_loop_active(d)


def trick_udp_loop(gate):
    """本地 TRICK 请求口(P0-1/T2.2): go2-audio 服务(pa_recv 独立进程)把 App 动作请求
    转成一行 "TRICK 名 时长" 发到 127.0.0.1:TRICK_PORT, 本线程调动作网关并把
    OK/LINKDOWN/NOTSTAND 回给它 —— 与拆分前的进程内 trick_cb 注入完全同口径。
    只绑回环: 外部网络碰不到; 名字过 config.TRICKS 白名单(与 pa_recv 的 App 侧检查对齐)。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", config.TRICK_PORT))
    except Exception as e:
        log("TRICK 本地口 %d 起不来, App 动作将不可用: %r" % (config.TRICK_PORT, e), lvl="error")
        return
    log("TRICK 本地口就绪: UDP 127.0.0.1:%d (go2-audio -> 桥)" % config.TRICK_PORT, lvl="debug")
    while True:
        try:
            data, addr = sock.recvfrom(256)
        except Exception:
            continue
        parts = data.decode("utf-8", "replace").strip().split(" ")
        r = "NOTSTAND"
        if (len(parts) == 3 and parts[0] == "TRICK"
                and parts[1] in config.TRICKS):
            try:
                r = gate(parts[1], int(parts[2])) or "NOTSTAND"
            except Exception as e:
                r = "NOTSTAND"
                log("TRICK 网关异常:", parts[1], repr(e), lvl="error")
        try:
            sock.sendto(r.encode("utf-8"), addr)
        except Exception:
            pass


def main():
    global dog, _ACTIVE

    def _on_term(signum, _frame):
        """systemd stop -> SIGTERM: 记 clean_stop 并写 .clean 停机标记, 下次启动
        就能区分"正常停止"和"掉电/被杀"(journal.last_stop)。"""
        try:
            journal.record("event", ev="clean_stop", sig=signum)
            _ev("sys", "clean_stop")
            journal.close(clean=True)
        finally:
            os._exit(0)

    try:
        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGINT, _on_term)
    except Exception:
        pass   # 非主线程/平台差异: 不挡启动

    # journal 黑匣子最先就位: 之后所有线程的记录才不会丢头几条; 失败只一行警告, 不挡启动
    if config.JOURNAL_ENABLED and not journal.init(config.JOURNAL_DIR or None,
                                                   config.JOURNAL_MAX_MB):
        log("警告: journal 黑匣子目录不可写, 本次运行不落盘", lvl="warn")
    else:
        _ev("sys", "boot", "last_stop=%s" % journal.last_stop())
        log("黑匣子上次停机标记:", journal.last_stop(), lvl="debug")
    link = DogLink()
    # 执行开关(ADR-0004): active 是唯一路径, 只建 FsmActive。
    # 先于 web/TRICK 线程构造, 启动期指令从第一条起就只有一个来源。
    dog = _make_controller(link, _T0)
    _ACTIVE = dog
    log("FSM 执行开关: active(FSM 产生指令, 旧路径已删)")
    def _trick_gate(name, dur):
        # 给 App 分别人话回包: 链路断(狗收不到) vs 条件不满足(未解锁/非行走/占用中)
        if state.link != "up":
            return "LINKDOWN"
        ok = dog.trick(name, dur)
        return "OK" if ok else "NOTSTAND"

    # P0-1/T2.2: 音频拆到独立服务 go2-audio(跑 pa_recv.py), 本进程不再 pa_recv.start(),
    # 也不注入回调; App 动作改走本地 UDP(TRICK_PORT), App "RC" 轮询由音频服务 HTTP 读 /state。
    # rc_loop 的 pa 形参仅留签名兼容(测试在用), 函数体不碰音频。
    threading.Thread(target=trick_udp_loop, args=(_trick_gate,), daemon=True).start()
    netrc.start(getattr(config, "NETRC_PORT", 15010), log,
                allow=getattr(config, "NETRC_ALLOW", []),
                token=getattr(config, "NETRC_TOKEN", ""))
    threading.Thread(target=web_server, args=(dog,), daemon=True).start()
    log("go2-bridge 启动: 网页端口=%d, SBUS=%s, 连狗方式=%s"
        % (config.WEB_PORT, config.SBUS_PORT, config.METHOD))
    rc_loop(dog, None)


if __name__ == "__main__":
    # 拆分架构生命线(2026-11-02 现场事故): 本文件作入口运行时模块名是 __main__,
    # link/webserver 的惰性 `import bridge` 会加载第二份副本(其 _ACTIVE/dog 全为 None)，
    # 症状=web arm 半途失效、axes 死路、closed 钩子空转，且 journal 无痕、webface 测试
    # (以模块方式 import)无法复现。把真身别名注册进 sys.modules，让惰性 import 拿到
    # 运行中的这一份。删此行=网页/网络控制静默残废。
    import sys as _sys
    _sys.modules["bridge"] = _sys.modules[__name__]
    try:
        main()
    except KeyboardInterrupt:
        pass
