# -*- coding: utf-8 -*-
"""DogFsm —— 遥控状态机(纯逻辑, P0-3 / 任务 T1.1)

把散在 bridge.py 的控制状态收成单一真身: Dog 类 + rc_loop 局部变量 + web 指令
分支 全部规则逐条搬入本文件, 每条注释标注来源行号。行号基准 = 提交 18a0e7a
(阶段 0) 的 bridge.py; T1.3 影子接线后 rc_loop 整体下移约 +193 行, 文中
rc_loop/web 行号仍按基准版标注(规则语义一致, 审计时按符号名定位)。

零硬件 / 零线程 / 零网络: 只依赖 time.monotonic(可注入 now)与 config 常量
(构造时可注入任意同名属性对象, 供表驱动测试换标定值)。

事件(输入) —— 由接线层(rc_loop 改造版)按原时序调用:
    frame(ch, fs, lost, source, now)   一帧解码结果(干净/坏), source="sbus"/"netrc"
    link(gen)                          DogLink 连上狗(state.link_gen += 1 时刻), 内含 Dog.reset 语义
    set_link(up)                       DogLink 会话状态镜像("up"/"down"), trick 网关分诊用
    tick(now)                          rc_loop 每循环一次: 延迟指令兜底 + 网页摇杆驱动
    safety(gap_clean, now)             rc_loop 每循环一次: 失控保护 + sbus_ok/clean 镜像
    web_cmd(cmd, ...)                  网页 /ws 指令: arm/disarm/damp/stand/walk/axes/closed
    trick(name, dur, now)              App 动作面板请求(经 15001 TRICK 口), 返回 (cmds, 回包字)
    serial_reopened()                  SBUS 串口(重)打开: 强制回到上锁态
    set_mode/set_gear/move/hold/damp   手动/调试入口(原 Dog 同名方法)

命令(输出) —— 每个事件返回本时刻应执行的动作列表, 接线层负责落地:
    ("submit", name, params)   发狗指令, 落地 = DogLink.submit(name, **params)
    ("relay", us)              PWM1 脉宽(us), 落地 = sv1.set_us(us)
    ("gpio", level)            四路继电器 IO 电平, 落地 = [s.set_level(level) for s in svs]
    ("log", msg)               桥日志, 落地 = log(msg)

submit 的三种接线(构造参数 send, 阶段3 HIGH 修复):
    send=None  表驱动测试/纯记账: 命令进 out, 恒成功(不看链路);
    send=探针  影子模式(FsmShadow 注入): 只回答"此刻 DogLink.submit 会不会
               入队成功", 不真发; 记账口径与 _RecLink 一致(请求即记) ——
               FSM 与旧 Dog 在"链路断时不提交本地状态"上同构;
    send=落地  active 模式(FsmActive 注入 link.submit): 事件内同步真发
               (指令日志/journal 由 DogLink.submit 自带), 不再进 out,
               接线层不会二次发送。
checked 语义(与旧 Dog 逐行对齐): set_mode 的 StandDown/StandUp/BalanceStand、
    _finish_rise 的 BalanceStand、trick/move/hold 的指令, 入队失败时**不提交**
    本地状态(mode/rising/moving/冷却), 请求标志留给下一帧重试 —— 绝不出现
    "StandUp 没发出去却进了 rising, 之后补 BalanceStand"的假起立。

与原实现的两个刻意差异(其余逐行等价):
  1. relay/gpio 只在目标值变化时输出 —— 与执行层去重语义对齐(servo.py:67 <25us
     不写盘 / servo.py:81 电平不变不写盘), 影子对比时输出序列即真实硬件变化。
  2. journal.record 埋点不移植(不属于命令类型): 影子期由 bridge 侧原样保留,
     切换后从 submit/log 命令流推导(kind=cmd 已等价, kind=event/frame 补在接线层)。
"""
import math
import time

import config


# ---- 纯函数: 与 bridge.py 同名函数一致(标定值读 cfg, 换遥控器重标只改 config) ----

def clamp01(v):
    """bridge.py:68-70 网页摇杆值钳到 -1..1"""
    v = float(v)
    return max(-1.0, min(1.0, v))


class DogFsm:
    """模式状态机: 0趴 1站 2走 3坐(Sit动作后的持续姿态)  [bridge.py:396-399 原注释]
    趴 -> 站/走: StandUp 后等 3s 补 BalanceStand(官方提示: StandUp 后关节可能锁死)
    坐 -> 走: C 拨下(静默, 狗已在低位不发指令) -> C 拨上(StandUp 起立)"""

    def __init__(self, cfg=None, now=None, send=None):
        self.cfg = cfg if cfg is not None else config
        self._send = send         # submit 接线: None=记账恒成功 / 探针 / 落地(见头注释)
        t0 = time.monotonic() if now is None else now
        # ---- rc_loop 局部变量(bridge.py:556-564) ----
        self.armed = False            # A 键解锁态(rc_loop:555)
        self.last_clean = t0          # 最近干净帧时刻: 失控保护计时锚点(:556)
        self.last_any = t0            # 最近任意帧(含坏帧)时刻: 只反映链路是否活着(:557)
        self.prev_high = None         # A 键上一帧电平(None=首帧只记录)(:559)
        self.last_src = None          # 帧来源(sbus/netrc): 换源首帧只记电平, 跨源差不合成边沿
        self.last_arm_edge = None     # A 解锁上升沿时刻(去抖锚点, 10-20; rc_loop 同名局部)
        self.last_gear = 0            # 拨档当前位(0=未同步; 趴下切挡只记账)(:560)
        self.gear_sent = 0            # 已真正发给狗的挡位(趴下不发送, 防步态弹狗)(:561)
        self.stand_prev = True        # C 键上一帧电平(首帧前视为稳态)(:562)
        self.stand_req = False        # C 上沿起立请求(被拦时保留每帧重试, 10-20)
        self.lie_req = False          # C 下沿趴下请求(同上)
        self.key_high = {}            # B/D 键电平记忆: 拨到高位的边沿才触发动作(:563)
        # ---- Dog 内部(bridge.py:401-407) ----
        self.mode = 0                 # 0趴 1站 2走 3坐
        self.moving = False
        self.cooldown_until = 0.0     # 花样动作播放截止(期间摇杆/新动作被屏蔽)
        self.pending = []             # [(触发时刻, 指令名)]
        self._t0 = t0                 # 时基锚点(进程启动时刻): 起立保护期+互锁总开关
        self._last_big = None         # 上一次大动作(StandUp/StandDown)执行时刻: 互锁计时
        # 状态驱动起立(10-20, 镜像 bridge.Dog): 等狗上报 body_height 稳定再补 BalanceStand
        self.rising = False           # 起立流程中(已发 StandUp, 等高度稳定)
        self.rise_t = 0.0             # 本次 StandUp 发出时刻
        self.rise_deadline = 0.0      # 超时兜底时刻
        self._h_hist = []             # [(包时间戳, body_height)] 最近 0.6s 高度样本
        self._h_ts = None             # 最近采样包的 height_t: 同一遥测包只记一次(P0-1)
        self._sync_pending = False    # (重)连后对账未决(P0-4): 等有效高度再定 mode
        # ---- State 镜像(bridge.py:94-108), FSM 属性即真身, /state 从这读 ----
        self.link_gen = 0             # 已处理的 WebRTC 连接代数(rc_loop:558)
        self.link_gen_cur = 0         # link 事件带来的最新代数(对应 state.link_gen)
        self.link_up = False          # 对应 state.link == "up"
        self.gear = None              # 已同步展示的挡位(未同步 None)(serial 重开清 None)
        self.action = None            # 动作锁: 正在播放的动作名
        self.action_end = 0.0         # 动作锁截止时刻
        self.web_armed = False
        self.web_vx = 0.0
        self.web_vy = 0.0
        self.web_vyaw = 0.0
        self.sbus_ok = False          # 链路活着(1s 内来过任意帧, 含坏帧)
        self.sbus_clean = False       # 数据干净(1s 内来过干净帧)
        # ---- relay/gpio 去重记忆(对齐 servo.py:67/81 执行层去重) ----
        self._relay_us = None         # servo 上电 duty=中点 1500us(servo.py:26), 首次必写
        self._gpio_lv = False         # GpioOut 上电=低(servo.py:109-110)
        self._drop_warn_t = None      # set_mode 入队失败告警限流锚点(5s 一条, 镜像旧 Dog)

    # ---- 内部: 指令发射与标定 ----

    def _emit(self, out, name, p):
        """发一条狗指令, 返回 True=已入队(或记账模式恒成功)。
        send=None: 命令进 out(接线层/测试消费); send 已注入: 委托之 —— 探针只探
        路不发送(记账归探针), 落地函数真发(out 不含 submit, 不会二次发送)。"""
        if self._send is None:
            out.append(("submit", name, p))
            return True
        return bool(self._send(name, **p))

    def _axis(self, raw, dz=0.06):
        """bridge.axis 同式: 通道原始值 -> -1..1, 中值/半行程 = cfg.AXIS_MID/AXIS_SPAN"""
        v = max(-1.0, min(1.0, (raw - self.cfg.AXIS_MID) / float(self.cfg.AXIS_SPAN)))
        if abs(v) < dz:
            return 0.0
        return (v - math.copysign(dz, v)) / (1 - dz)

    def _pos(self, raw, n=3):
        """bridge.pos 同式: n 段开关原始值 -> 0..n-1, 行程 = cfg.POS_MIN/POS_MAX"""
        return min(n - 1, max(0, round((raw - self.cfg.POS_MIN)
                                       / float(self.cfg.POS_MAX - self.cfg.POS_MIN)
                                       * (n - 1))))

    # ---- 内部: 延迟指令到点发送 ----

    def _drain_pending(self, now):
        """Dog.tick(bridge.py 同名): 到点的延迟指令(预留通道)"""
        out = []
        due = [t for t in self.pending if now >= t[0]]
        if due:
            self.pending = [t for t in self.pending if now < t[0]]
            for _, name in due:
                self._emit(out, name, {})   # 旧路径 fire-and-forget(bridge.py:1143)
        return out

    def _begin_rise(self, now, out):
        """Dog._begin_rise(bridge.py 同名): 发 StandUp, 进入等高度稳定状态。
        返回 False = StandUp 没入队 —— 不得进入 rising(否则会有假起立,
        之后 _maybe_balance 补的 BalanceStand 会打到趴着的狗上)。"""
        if not self._emit(out, "StandUp", {}):
            return False
        self.rising = True
        self.rise_t = now
        self.rise_deadline = now + self.cfg.STAND_TIMEOUT_S
        self._h_hist = []
        self._h_ts = None
        return True

    def _finish_rise(self, now, why, height=None):
        """Dog._finish_rise(bridge.py 同名): 补 BalanceStand 并解除 rising。
        BalanceStand 没入队 -> 保持 rising 下一 tick 重试(旧 Dog 镜像)。
        日志由接线层(bridge 旧路径)记, 影子输出只保留可落地命令(与旧实现一致)。"""
        out = []
        if not self._emit(out, "BalanceStand", {}):
            return out
        self.rising = False
        self._h_hist = []
        self._h_ts = None
        return out                                       # why=stable/no_telemetry/timeout

    def _stand_failed(self, now, height):
        """Dog._stand_failed(bridge.py 同名): 到点仍低位, 不补 BalanceStand,
        本地模式同步回趴, 等用户重新拨 C(P0-2)。"""
        self.rising = False
        self._h_hist = []
        self._h_ts = None
        self.mode = 0
        self.moving = False
        return []                                        # 零输出: 绝不发 BalanceStand

    def _maybe_balance(self, now, height=None, height_t=None):
        """Dog._maybe_balance(bridge.py 同名, P0-1/P0-2 同步):
        不同遥测包 >=3 个、首样本年龄 >=0.5s、高度稳定且高于阈值 -> BalanceStand;
        无遥测 -> STAND_TIMEOUT_S 才兜底; 到点仍低位 -> 起立失败(不发 BalanceStand)。"""
        if not self.rising:
            return []
        if self.mode == 3:
            # P1: 坐姿没有"起立中"; 迟到的 BalanceStand 会把坐姿拉起来
            self.rising = False
            self._h_hist = []
            self._h_ts = None
            return []
        fresh = (height is not None and height_t is not None
                 and now - height_t < self.cfg.DOG_STATE_STALE_S)
        if not fresh:
            if now >= self.rise_deadline:
                return self._finish_rise(now, "no_telemetry")   # why=no_telemetry
            return []
        if height_t != self._h_ts:
            # 只在"新遥测包"到达时记样本; 同一包被 20Hz tick 重复读到不算(P0-1 根因)
            self._h_ts = height_t
            self._h_hist.append((height_t, height))
        self._h_hist = [(t, v) for t, v in self._h_hist if now - t <= 0.6]
        if (len(self._h_hist) >= 3 and now - self._h_hist[0][0] >= 0.5
                and height > self.cfg.DOG_STAND_HEIGHT
                and max(v for _, v in self._h_hist)
                - min(v for _, v in self._h_hist) < self.cfg.DOG_HEIGHT_STABLE):
            return self._finish_rise(now, "stable", height)     # why=stable
        if now >= self.rise_deadline:
            if height < self.cfg.DOG_STAND_HEIGHT:
                return self._stand_failed(now, height)          # P0-2: 不发 BalanceStand
            return self._finish_rise(now, "timeout", height)    # why=timeout
        return []

    def _apply_reconcile(self, now, first, height=None, height_t=None):
        """Dog._apply_reconcile(bridge.py 同名, P0-4): 新鲜高度 + 滞回对账本地 mode,
        中间区不臆测; 首连无有效数据才回落"假定站立"。"""
        if not self._sync_pending or self.rising or now < self.cooldown_until:
            return []
        fresh = (height is not None and height_t is not None
                 and now - height_t < self.cfg.DOG_STATE_STALE_S
                 and self.cfg.DOG_HEIGHT_MIN <= height <= self.cfg.DOG_HEIGHT_MAX)
        if not fresh:
            if first:
                self.mode = 1     # 首连无有效数据: 回落旧行为(日志由接线层记, 不比)
            return []
        if height > self.cfg.DOG_STAND_LATCH_H:
            self.mode = 1
        elif height < self.cfg.DOG_LIE_LATCH_H:
            self.mode = 0
        else:
            return []                    # 滞回中间区: 不猜, 等下一包
        self._sync_pending = False
        return [("log", "遥测对账: 高度 %.3fm -> mode=%d" % (height, self.mode))]

    def _set_relay(self, lv):
        """B 键 -> 继电器(bridge.py:602-607): 高位 1950us 吸合 / 低位 1050us 断开,
        四路 IO 电平并联跟随; 只在变化时出命令(对齐 servo.py:61-90 执行层去重)。
        gap 失控安全关断(bridge.py:685-688)也走这里(level=False)。"""
        out = []
        us = 1950 if lv else 1050
        if self._relay_us is None or abs(us - self._relay_us) >= 25.0:
            self._relay_us = us
            out.append(("relay", us))
        if self._gpio_lv != lv:
            self._gpio_lv = lv
            out.append(("gpio", lv))
        return out

    # ---- Dog 同名方法(bridge.py:396-539) ----

    def set_mode(self, m, now=None):
        """Dog.set_mode(bridge.py 同名) —— 返回 (ok, cmds): ok=True=已执行或本来就该
        状态; False=被互锁/起立保护期/坐姿规则拦下(未发指令, 状态未动), 与
        bridge.Dog.set_mode 返回值同义(10-20 供电口事故止血)"""
        now = time.monotonic() if now is None else now
        out = []
        if m not in (0, 1, 2, 3):
            # 调用方全是字面量, 此门纯防御: -1 会静默穿过元组负索引污染 mode
            raise ValueError("set_mode: 非法模式 %r(只收 0趴/1站/2走/3坐)" % (m,))
        if m == self.mode:
            return True, out
        # 起立流程中 1->2 只是目标标签对齐(bridge.Dog 同款守护 10-31): 清了 rising 会让
        # BalanceStand 补发永久丢失, 且下一帧步态同步会插进物理 StandUp(10-21 断电模式)
        if self.rising and m == 2 and self.mode == 1:
            return True, out
        # 坐姿规则(P1, bridge.Dog 同步): 坐着时 C 在上位不算数, 提前返回且不发任何
        # 指令 —— 否则 C 停在上位时每帧一条 StopMove(20Hz 刷指令队列)
        if self.mode == 3 and m == 2:
            return False, out
        # 大动作 = 趴0 <-> 站/走1|2 之间切换(会发 StandUp 或 StandDown);
        # 坐3 本在低位: 3->0 静默 / 3->1 走 BalanceStand, 都不算大动作
        big = (m == 0) != (self.mode == 0)
        # 互锁/保护期只拦"起立"(m!=0): 上锁/趴下任何时候都必须放行(10-20 复核)
        if big and m != 0 and self._t0 is not None:
            if m != 0 and now - self._t0 < self.cfg.STAND_GUARD_S:
                # 起立保护期(10-20): 拒绝任何会发 StandUp 的请求
                # (旧路径侧记 journal ev=stand_guard + log, 影子期不重复)
                return False, out
            if (self._last_big is not None
                    and now - self._last_big < self.cfg.MODE_DWELL_S):
                # 大动作互锁(10-20): 两次起立/趴下太近会触发狗供电口保护性断电
                # (旧路径侧记 journal ev=interlock + log, 影子期不重复)
                return False, out
        # 模式一变就作废未到点的延迟指令与起立流程(bridge.py 原注释): 起立过程中拨 C
        # 趴下时, 迟到的 BalanceStand 会把趴着的狗瞬间弹起(10-17 实测危险)
        self.pending = []
        self.rising = False
        self._h_hist = []
        self._h_ts = None
        if self.mode == 3 and m == 0:
            # 坐->趴(bridge.py:417-426 原注释): 狗本来就在低位, 只记状态不发指令
            # (发 StandDown 会从坐姿猛塌)
            self.mode = 0
            self.moving = False
            self.cooldown_until = 0.0
            self.action = None
            self._sync_pending = False
            out.append(("log", "模式 -> 趴"))
            return True, out
        self._emit(out, "StopMove", {})                  # bridge.py:427(旧路径不查返回)
        ok = True
        if m == 0:
            ok = self._emit(out, "StandDown", {})        # bridge.py:428-429(checked)
        elif m == 1:
            ok = self._begin_rise(now, out) if self.mode == 0 \
                else self._emit(out, "BalanceStand", {})  # bridge.py:434-435(checked)
        elif m == 2:
            if self.mode == 0:
                ok = self._begin_rise(now, out)          # checked: StandUp 没入队不进 rising
        if not ok:
            # 入队失败(链路断/事件循环收摊, bridge.py:931-939 镜像): 不提交本地
            # 状态, 请求标志下一帧重试; 告警限流 5s(旧路径同款, journal 归接线层)
            if self._drop_warn_t is None or now - self._drop_warn_t >= 5.0:
                self._drop_warn_t = now
                out.append(("log", "指令未入队(链路/循环不可用), 模式保持 %d, 等重试"
                            % self.mode))
            return False, out
        self.mode = m
        self.moving = False
        self.cooldown_until = 0.0
        self.action = None
        self._sync_pending = False      # 用户已明确指定模式, 不再用遥测覆盖(P0-4)
        out.append(("log", "模式 -> " + ("趴", "站", "走", "坐")[m]))   # bridge.py:448
        if big and self._t0 is not None:
            self._last_big = now
        return True, out

    def damp(self):
        """Dog.damp(bridge.py:450-459 原注释): 应急泄力趴窝(已弃用: 自动失控保护改走
        set_mode(0) 缓降, 函数保留仅供手动)。旧路径 submit 不查返回, 镜像之。"""
        out = []
        self._emit(out, "Damp", {})
        self.mode = 0
        self.moving = False
        self.cooldown_until = 0.0
        self.action = None
        return out

    def set_gear(self, g):
        """Dog.set_gear(bridge.py:461-469 原注释): 三档拨档 = 步态切换(实测本固件
        SpeedLevel 只收 -1/1, 三档速度实为步态): 1=FreeWalk(慢) 2=FreeWalk 3=TrotRun
        (10-21 用户定档: 挡1 弃经济步态, 1/2 同步态只差限速)。
        fire-and-forget(旧路径 bridge.py:1085 同构): gear_sent 在 frame() 里先记后发,
        发送失败不重试 —— 断链窗口丢一次挡位, 恢复后同挡不补发, 换挡才同步
        (继承行为, 两路径一致, 要改需一起改并重走影子验证)。"""
        gait = {1: "FreeWalk", 2: "FreeWalk", 3: "TrotRun"}.get(g)
        if gait:
            out = []
            self._emit(out, gait, {})   # 旧路径 fire-and-forget(bridge.py:1085)
            return out + [("log", "挡位 -> %d (%s)" % (g, gait))]
        return []

    def trick(self, name, dur, now=None):
        """Dog.trick(bridge.py:471-491) + App 动作网关分诊(bridge.py:808-812)。
        原注释: 仅走模式(站立)可触发; 播放期(冷却)内摇杆/新动作被屏蔽。
        返回 (cmds, result), result: "OK"=已触发 / "NOTSTAND"=条件不满足 /
        "LINKDOWN"=链路断(submit 会被静默丢弃, 不能只收冷却不认账, bridge.py:476)"""
        now = time.monotonic() if now is None else now
        out = []
        if not self.link_up:                      # bridge.py:810-811
            return out, "LINKDOWN"
        # P1: 起立流程未完成不得插动作(与 bridge.Dog.trick 的 rising 门同步)
        if (self.mode != 2 or self.rising or self.pending
                or now < self.cooldown_until):                        # bridge.py:477
            return out, "NOTSTAND"
        if not self._emit(out, name, {}):
            # submit 没入队: 不收冷却不认账(旧 Dog.trick bridge.py:1099-1100 镜像,
            # 否则谎报成功, 整段冷却期动作面板全被锁死)
            return out, "LINKDOWN"
        self.cooldown_until = now + dur
        self.action = name                        # 镜像给网页端显示动作锁(bridge.py:481)
        self.action_end = now + dur
        out.append(("log", "动作 -> %s (%ds)" % (name, dur)))
        if name == "Sit":
            # 坐下是持续姿态(bridge.py:485-490 原注释): 记成独立状态,
            # C 拨下(静默)->拨上才会再起立
            self.mode = 3
            out.append(("log", "模式 -> 坐"))
        return out, "OK"

    def move(self, vx, vy, vyaw, now=None):
        """Dog.move(bridge.py 同名): 仅走模式、起立流程已完成且无 pending/冷却时响应。
        入队成功才认账 moving(bridge.py:1120-1125 镜像), 失败下一帧重试。"""
        now = time.monotonic() if now is None else now
        out = []
        if self.mode != 2 or self.rising or self.pending or now < self.cooldown_until:
            return out
        if (vx, vy, vyaw) != (0.0, 0.0, 0.0):
            if self._emit(out, "Move",
                          {"x": round(vx, 3), "y": round(vy, 3), "z": round(vyaw, 3)}):
                self.moving = True
        elif self.moving and self._emit(out, "StopMove", {}):
            self.moving = False
        return out

    def hold(self):
        """Dog.hold(bridge.py:504-509 原注释): 失控保护第一步, 幂等停走"""
        out = []
        if self.moving and self._emit(out, "StopMove", {}):
            self.moving = False
        return out

    # ---- 事件 ----

    def link(self, gen, height=None, height_t=None):
        """DogLink 连上狗 -> dog.reset()。
          开机首连: 一条指令都不发; 本地 mode 用新鲜高度对账(10-20/P0-4),
            无数据才回落假定站立; 锁定等解锁
          断链重连: 只发 StopMove 停走保持姿态, 本地 mode 同样用新鲜高度对账
            (高->1/低->0/中间不猜), 不对已站立的狗发 StandUp"""
        out = []
        if gen != self.link_gen_cur:
            self.link_gen_cur = gen
            self.link_up = True
            first = gen <= 1                      # bridge.py:527 state.link_gen <= 1
            now = time.monotonic()
            if not first:
                self._emit(out, "StopMove", {})   # 断链重连停走(旧路径不查返回, bridge.py:1157)
            self.moving = False
            self.cooldown_until = 0.0
            self.pending = []
            self.rising = False
            self._h_hist = []
            self._h_ts = None
            self.action = None
            self._sync_pending = True
            out += self._apply_reconcile(now, first, height, height_t)
            out.append(("log", "开机首连: 已锁定等待解锁(不发任何指令)" if first
                        else "断链重连: 停走保持姿态, 请重新解锁"))
        return out

    def set_link(self, up):
        """DogLink 会话状态镜像(bridge.py:96 state.link "up"/"down"): 断链时接线层
        调 set_link(False), trick 网关据此回 LINKDOWN(bridge.py:477, 810-811)"""
        self.link_up = bool(up)
        return []

    def serial_offline(self):
        """SBUS 串口打不开/掉线时接线层调用: 链路健康镜像置脏, 与
        bridge.py 旧路径 state.sbus_ok/sbus_clean=False 同步(否则切 live 后
        /state 会在无串口时误报链路正常)"""
        self.sbus_ok = False
        self.sbus_clean = False
        return []

    def serial_reopened(self):
        """SBUS 串口(重)打开(bridge.py:569-575 原注释): 必须重新解锁, 防插拔误触发。
        注意 last_gear(记账)按原实现保留不重置, 只清展示镜像 gear=None —— 与
        rc_loop 局部变量行为一致(重开后再拨回同挡, state.gear 不更新)"""
        self.armed = False
        self.prev_high = None
        self.stand_req = False      # 全新会话: 撤销挂起的姿态请求(10-20, 与 rc_loop 同步)
        self.lie_req = False
        self.gear = None
        self.sbus_ok = False
        self.sbus_clean = False
        return []

    def frame(self, ch, fs, lost, source="sbus", now=None, height=None, height_t=None):
        """一帧遥控数据(bridge.py:586-669)。ch=16 通道原始值, fs/lost=接收机
        失控/丢帧标志, source="sbus"/"netrc"(journal 用, 这里只记账)。
        原时序: 干净帧分支开头先 d.tick()(:600), 再继电器/A/C/挡位/摇杆;
        坏帧只 hold 不作控制输入(:591-596)。"""
        now = time.monotonic() if now is None else now
        if fs or lost:
            # 坏帧(bridge.py:591-596 原注释): 链路活着但数据脏, 不作控制输入;
            # 立即站定等待恢复(幂等); 不上锁不趴下
            self.last_any = now                   # :594
            if self.armed:
                return self.hold()                # :595-596 d.hold()
            return []
        self.last_clean = now                     # :598
        self.last_any = now                       # :599
        if source != self.last_src:
            # 换源首帧只记电平不当边沿(bridge.rc_loop 同款 10-31): App 与实体遥控同时
            # 在线时, 跨源电平差不得合成 A/C/B 边沿(防丢包窗口振荡 / 切后台回来自动起立)
            self.last_src = source
            self.prev_high = None
            self.stand_prev = ch[self.cfg.CH_STAND] > self.cfg.STAND_ON
            self.key_high = {cid: ch[cid] > self.cfg.KEY_THRESHOLD
                             for cid in self.cfg.KEY_ACTIONS}
        out = self._apply_reconcile(now, False, height, height_t)  # 重连对账未决时续判
        out += self._maybe_balance(now, height, height_t)   # 状态驱动起立判定(same tick)
        out += self._drain_pending(now)           # :600 d.tick() —— 必须在帧处理前:
        #                                           set_mode 若清 pending, 迟到指令不得先漏发
        # 载荷输出(bridge.py:601-607 原注释): B键->15脚pwm1两档脉宽(1650分界的
        # RC解码继电器)+IO电平
        lv = ch[self.cfg.CH_RELAY] > self.cfg.RELAY_ON_RAW      # :602
        out += self._set_relay(lv)                # :604-606 set_us(1950/1050) + set_level(lv)
        # A 键: 解锁/上锁(bridge.py:610-636)
        a_high = ch[self.cfg.CH_ARM] > self.cfg.ARM_ON          # :610
        stand = ch[self.cfg.CH_STAND] > self.cfg.STAND_ON       # :637 C 电平每帧都算(上锁期也跟)
        if self.link_gen != self.link_gen_cur:    # :611 (重)连上狗: 回到上锁态, 防重连瞬间狗自行动作
            self.link_gen = self.link_gen_cur     # :612
            self.stand_req = False                # 撤销挂起的姿态请求, 防重解锁后自动执行(10-20)
            self.lie_req = False
            if self.armed:
                self.armed = False                # :614
                out += self.hold()                # :616 停走保持姿态, 不趴下(09-29 用户要求)
                out.append(("log", "链路重连, 已回上锁态(保持站立), 请重新解锁"))  # :618
            self.prev_high = a_high               # :619
        if self.prev_high is None:                # :620-621 首帧只记录电平, 不判定解锁边沿
            self.prev_high = a_high
        if self.armed and not a_high:
            # A 拨到上锁位(:622-628): 上锁 + 趴下(下降沿永不去抖, 10-20)
            self.armed = False
            self.stand_req = False                # 上锁即撤销挂起的姿态请求(解锁不做自动动作)
            self.lie_req = False
            out += self.set_mode(0, now=now)[1]   # :625
            out.append(("log", "已上锁(RC)"))     # :628
        elif not self.armed and a_high and not self.prev_high:
            # 翻拨边沿解锁(:629-635 原注释): 只认翻拨动作, A 一直停在解锁位不算
            # (连发/持续高电平不误判 —— prev_high 每帧更新, 平稳高电平无边沿)。
            # A 去抖(10-20): 距上次解锁边沿 < ARM_DEBOUNCE_S 忽略本次(边沿仍刷新锚点;
            # 旧路径侧记 journal ev=arm_debounced, 影子期不重复)
            if (self.last_arm_edge is not None
                    and now - self.last_arm_edge < self.cfg.ARM_DEBOUNCE_S):
                pass
            else:
                self.armed = True                 # :630
                self.web_armed = False            # :632 RC 接管, 网页控制让位
                out.append(("log", "已解锁(RC)"))  # :635
                if self.mode != 3 and stand:
                    # 解锁即可控(10-31, 与 bridge 同步): C 已在上位则视同 C 上沿起立请求,
                    # 省掉"解锁后再拨一次 C"; C 在下位或坐姿则不动
                    self.stand_req = True
            self.last_arm_edge = now
        self.prev_high = a_high                   # :636
        if self.armed:
            # 姿态请求标志+每帧重试(10-20 供电口事故): C 边沿只置标志, set_mode 被互锁/
            # 起立保护期拦下(返回 False)时标志保留, 到点自动重试; C 回对面电平则撤销。
            # 趴下只认 C 上->下边沿(旧的"armed 且 C 在下每帧自动趴"路径已删, 解锁不再
            # 自动做任何动作); C 稳在上的补同步保留(开机首连 mode=1, 解锁即可控)
            if stand and not self.stand_prev:
                self.stand_req = True             # C 下->上边沿: 起立请求
            if not stand and self.stand_prev:
                self.lie_req = True               # C 上->下边沿: 趴下请求
            handled = False
            if self.stand_req:
                handled = True
                if not stand:
                    self.stand_req = False        # C 已回下位: 撤销起立请求
                else:
                    ok, cmds = self.set_mode(2, now=now)
                    out += cmds
                    if ok:
                        self.stand_req = False
            if self.lie_req:
                handled = True
                if stand:
                    self.lie_req = False          # C 已回上位: 撤销趴下请求
                else:
                    ok, cmds = self.set_mode(0, now=now)
                    out += cmds
                    if ok:
                        self.lie_req = False
            if not handled and stand and self.mode != 0:
                out += self.set_mode(2, now=now)[1]   # C 稳在上且狗本是站/走: 补同步模式
            g = self._pos(ch[self.cfg.CH_GEAR], 3) + 1  # :648 三档拨档 -> 挡1/2/3
            if g != self.last_gear:
                self.last_gear = g                # :649-650
                self.gear = g                     # :651 展示镜像
            # 步态只在站立时同步(:652-656 原注释): 趴着切挡(含趴着解锁, 无论 C 在上
            # 在下)只记账, 否则 TrotRun 等步态指令会把趴下的狗直接弹起来(10-10 实测 bug)。
            # 10-21: 起立流程(rising)期间也不发 —— StandUp 与 EconomicGait 并发实测触发
            # 上装断电, 等 BalanceStand 完成后的下一帧再同步
            if stand and self.mode != 0 and not self.rising and self.gear_sent != g:
                self.gear_sent = g                # :655
                out += self.set_gear(g)           # :656
            vx_max, vy_max, vyaw_max = self.cfg.GEAR_SPEEDS.get(
                g, (self.cfg.VX_MAX, self.cfg.VY_MAX, self.cfg.VYAW_MAX))  # :657-658
            ax = self._axis(ch[self.cfg.CH_PITCH]) * self.cfg.SIGN_PITCH   # :659
            ay = self._axis(ch[self.cfg.CH_ROLL]) * self.cfg.SIGN_ROLL     # :660
            az = self._axis(ch[self.cfg.CH_YAW]) * self.cfg.SIGN_YAW       # :661
            # B/D 键动作(:662-667 原注释): 拨到高位的边沿触发一次, 摇杆回中时才响应
            # (当前 config.KEY_ACTIONS={} 已退役, 逻辑保留)
            for cid, (name, dur) in self.cfg.KEY_ACTIONS.items():
                high = ch[cid] > self.cfg.KEY_THRESHOLD  # :664(标定值不再写死)
                if high and not self.key_high.get(cid) and not (ax or ay or az):
                    cmds, _r = self.trick(name, dur, now=now)        # :666
                    out += cmds
                self.key_high[cid] = high         # :667
            out += self.move(ax * vx_max, ay * vy_max, az * vyaw_max, now=now)  # :668
        self.stand_prev = stand   # :669 每帧更新(含上锁期): 解锁瞬间才不会把陈旧电平当边沿
        return out

    def tick(self, now=None, height=None, height_t=None):
        """rc_loop 每循环一次(bridge.py 无帧/坏帧时 d.tick() 的兜底,
        干净帧时 frame() 内已做过, 此处幂等) + 网页摇杆驱动"""
        now = time.monotonic() if now is None else now
        out = self._apply_reconcile(now, False, height, height_t)  # 重连对账未决时续判
        out += self._maybe_balance(now, height, height_t)
        out += self._drain_pending(now)
        # 网页端(:700-704 原注释): 仅 RC 上锁时接管
        if not self.armed and self.web_armed:
            out += self.move(self.web_vx * self.cfg.VX_MAX,
                             self.web_vy * self.cfg.VY_MAX,
                             self.web_vyaw * self.cfg.VYAW_MAX, now=now)
        return out

    def safety(self, gap_clean, now=None):
        """rc_loop 每循环一次(bridge.py:673-698): sbus 状态镜像 + 失控保护。
        gap_clean = now - last_clean, 由接线层按同式算好传入"""
        now = time.monotonic() if now is None else now
        out = []
        self.sbus_ok = (now - self.last_any) < 1.0     # :674 链路活着(坏帧也算)
        self.sbus_clean = gap_clean < 1.0               # :675 数据干净
        if gap_clean > self.cfg.FS_STOP_S:              # :684 脏/丢帧
            out += self._set_relay(False)               # :685-688 输出安全关断(脉宽1050+IO低), 防断联冻结在开
            if self.armed:
                out += self.hold()                      # :690-691 站定等待恢复
                if gap_clean > self.cfg.FS_DAMP_S:      # :692 持续 30s 无干净帧: 真失联
                    out.append(("log", "RC 失联 %.0fs, 上锁停走(保持姿态, 09-29 起不再趴下)"
                                % self.cfg.FS_DAMP_S))  # :695
                    self.armed = False                  # :696
                    self.stand_req = False   # 上锁即撤销挂起的姿态请求(10-20, 与其他上锁路径同语义)
                    self.lie_req = False
                    out += self.hold()                  # :698 停走即可, 姿态交给狗自己保持(不趴下)
        return out

    def web_cmd(self, cmd, vx=None, vy=None, vyaw=None, now=None):
        """网页 /ws 指令(bridge.py:746-767) + 连接断开(:2221-2231)。
        cmd: "arm"/"disarm"/"damp"/"stand"/"walk"/"axes"(摇杆值)/"closed"(ws 断开)"""
        now = time.monotonic() if now is None else now
        out = []
        if cmd == "axes":
            # 只更新带来的键, 缺键不清零(10-31): 网页双摇杆各发各的键才能边走边转;
            # 心跳{ping:1}三键全缺, 天然不动
            if vx is not None:
                self.web_vx = clamp01(vx)
            if vy is not None:
                self.web_vy = clamp01(vy)
            if vyaw is not None:
                self.web_vyaw = clamp01(vyaw)
            return out
        if self.armed:
            return out                       # :748-750 RC 优先, 网页指令忽略
        if cmd == "arm":
            self.web_armed = True            # :751-753
            # 统一(10-31, 与 bridge 同步): arm 直接到可控, 不再先到站立平衡
            out += self.set_mode(2, now=now)[1]  # 被互锁/保护期拦则无输出
        elif cmd in ("disarm", "damp"):
            self.web_armed = False            # :756-758
            out += self.set_mode(0, now=now)[1]      # :759
        elif cmd in ("stand", "walk"):
            # P1: 起立流程未完成时网页 stand/walk 明确拒绝(接线层回 error),
            # 不清 rising 也不放行 Move; 趴下(disarm/damp)永远允许
            if not self.rising:
                out += self.set_mode(1 if cmd == "stand" else 2, now=now)[1]
        elif cmd == "closed":
            # :2221-2231 网页端断开, 解除网页控制并停走保持姿态(10-29 起:
            # 趴下只认显式命令; 关网页就趴下在楼梯/斜坡/货架边是摔机风险)
            self.web_armed = False
            self.web_vx = self.web_vy = self.web_vyaw = 0.0
            if not self.armed and self.link_up:   # 与桥同款: 断链时不发
                out += self.hold()                # 幂等 StopMove, mode 不动
        return out
