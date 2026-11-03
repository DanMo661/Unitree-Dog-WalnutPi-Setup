# -*- coding: utf-8 -*-
"""遥测/日志/共享状态(State 单例 + 内存日志环 + 事件环 + 狗姿态有效性), 从 bridge.py 平移, 行为零变更"""
import collections
import time

import config
import journal

_LOG_BUF = collections.deque(maxlen=600)   # 内存日志环: /logs 端点给控制台日志面板用
_J_EVERY_N = max(1, int(config.JOURNAL_EVERY_N))   # journal 高频采样比(帧/Move), 防误配 0
_T0 = time.monotonic()    # 进程启动锚点: /state 的 uptime_s 用


def log(*a, lvl=""):
    tag = ("[%s] " % lvl) if lvl else ""
    # 时区: 板子系统时钟是 UTC, 按 config.LOG_TZ_OFFSET_H 换算成当地时间再显示
    stamp = time.strftime("[%H:%M:%S]",
                          time.gmtime(time.time() + config.LOG_TZ_OFFSET_H * 3600))
    line = stamp + " " + tag + " ".join(str(x) for x in a)
    _LOG_BUF.append(line)
    print(line, flush=True)


_EVENTS = collections.deque(maxlen=10)   # /state 最近事件(调试台"最近事件"面板)


def _ev(kind, ev, detail=""):
    """记一条界面事件(内存环): 与控制/journal 解耦, 失败静默。"""
    try:
        _EVENTS.append({"ts": time.time(), "kind": str(kind),
                        "ev": str(ev), "detail": str(detail)})
    except Exception:
        pass


class State:
    """线程间共享的展示/交接状态(单属性赋值, GIL 下够用)"""
    _link = "init"        # init / connecting / up / down / lib_missing (真身, link 经 property 记变化)
    link_gen = 0         # WebRTC 成功连接的代数, rc_loop 感知重连用
    mode = 0             # 0趴 1站 2走
    rc_armed = False
    sbus_ok = False       # 链路活着(1s 内来过任意帧, 含坏帧)
    sbus_clean = False    # 数据干净(1s 内来过干净帧)
    web_armed = False
    web_vx = 0.0
    web_vy = 0.0
    web_vyaw = 0.0
    gear = None           # 已同步给狗的挡位 1/2/3(未同步 None)
    action = None         # 动作锁: 正在播放的动作名(Dog.trick 成功触发时记录)
    action_end = 0.0      # 动作锁截止时刻(monotonic, 推送前换算成剩余秒)
    last_frame_t = None   # 最近任意帧(含坏帧/网络遥控源)的 monotonic 时刻, /state 的 frame_age 用
    relay_on = False      # 继电器目标状态(rc_loop 每次 sv1.set_us 后镜像, B 键高位=True)
    relay_us = None       # 最近一次请求的 pwm1 脉宽目标 us(1050/1950; 退化硬件 cur_us 恒 None, 记目标值)

    # ---- 狗真实姿态(rt/sportmodestate 订阅, 10-20): 控制决策与监控台都以狗上报为准 ----
    dog_mode = None       # 狗上报的运动模式(原始值, 0=idle 1=trot...)
    dog_height = None     # body_height 米; 站约 0.28~0.33, 趴约 0.08~0.15
    dog_gait = None       # gait_type 原始值
    dog_vel = None        # [vx, vy, wz] 狗自报速度
    dog_state_t = None    # 最近一条 sportmodestate 的 monotonic 时刻(stale 判定)
    dog_state_n = 0       # 收到状态包计数(诊断"订阅到底通没通")
    # ---- GO2 信息卡(rt/lf/lowstate + mainboardstate, 10-31): 只读监控, 不碰控制 ----
    dog_joints = None     # 关节温度列表(°C, 槽位序: FR/FL/RR/RL 各 髋/大腿/小腿)
    dog_bms = None        # 电池摘要 dict(SOC/current/voltage/temperature, lowstate 自带)
    dog_mb = None         # 主板/机身温度(°C, mainboardstate.temperature_ntc1)
    dog_imu_t = None      # IMU 温度(sportmodestate.imu_state.temperature 原值, 单位待校准)
    dog_rpy = None        # IMU 姿态角[横滚,俯仰,偏航](rad, sportmodestate.imu_state.rpy)
    dog_err = None        # 狗错误码(sportmodestate.error_code 原值; 语义待映射)
    dog_low_t = None      # 最近一条 lowstate 的 monotonic 时刻(新鲜度)
    dog_low_n = 0         # lowstate 包计数
    dog_mb_n = 0          # mainboardstate 包计数(首包日志用, 该话题可能不发)
    last_src = "sbus"     # 最近控制帧来源: sbus / netrc
    last_cmd = None       # 最近一条非 Move 指令 {name, ts, result}
    last_big_sent = None  # 最近一次大动作(StandUp/StandDown)真实发往狗的时刻(队列互锁用)
    arm_blocked_until = 0.0  # A 键去抖窗口截止时刻(monotonic, /state 的 arm_blocked 用)
    loop_ts = None        # 事件循环最近打点时刻(DogLink._loop_ticker 刷新; rc_loop 据此报停摆)

    @property
    def link(self):
        """journal 埋点: 对外仍是普通属性读写, 仅 link 值变化时记一条 kind=link"""
        return self._link

    @link.setter
    def link(self, v):
        if v != self._link:
            self._link = v
            journal.record("link", link=v)
            _ev("link", v)


state = State()


def _set_cmd_result(name, result):
    """更新最近一条非 Move 指令的发送结果(供 /state.last_cmd)。"""
    lc = state.last_cmd
    if isinstance(lc, dict) and lc.get("name") == name:
        lc["result"] = result


def _dog2_snapshot(now):
    """GO2 信息卡数据(10-31): lowstate 关节温度/电池 + mainboardstate 机身温度。
    只读镜像, 订阅没数据时字段为 None, 绝不抛。"""
    age = (round(now - state.dog_low_t, 2)
           if state.dog_low_t is not None else None)
    return {"joints": state.dog_joints, "bms": state.dog_bms,
            "mb": state.dog_mb, "imu_t": state.dog_imu_t,
            "rpy": state.dog_rpy, "err": state.dog_err,
            "low_age": age, "n": state.dog_low_n,
            "fresh": age is not None and age < 5.0}


def _dog_height_valid(now=None):
    """狗上报高度是否可采信: 新鲜 + 落在合理范围(0.05~0.50m)。
    None/超范围都算未知 —— 重连对账宁可等下一包也不臆测(P0-4)。"""
    now = time.monotonic() if now is None else now
    h, ts = state.dog_height, state.dog_state_t
    return (h is not None and ts is not None
            and now - ts < config.DOG_STATE_STALE_S
            and config.DOG_HEIGHT_MIN <= h <= config.DOG_HEIGHT_MAX)


def clamp01(v):
    v = float(v)
    return max(-1.0, min(1.0, v))
