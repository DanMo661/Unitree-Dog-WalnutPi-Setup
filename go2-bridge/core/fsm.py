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

与原实现的两个刻意差异(其余逐行等价):
  1. relay/gpio 只在目标值变化时输出 —— 与执行层去重语义对齐(servo.py:67 <25us
     不写盘 / servo.py:81 电平不变不写盘), 影子对比时输出序列即真实硬件变化。
  2. journal.record 埋点不移植(不属于命令类型): 影子期由 bridge 侧原样保留,
     切换后从 submit/log 命令流推导(kind=cmd 已等价, kind=event/frame 补在接线层)。
"""
import math
import time

import config


# ---- 纯函数: 与 bridge.py 同名函数逐行一致(P1-3 才配置化, 此处保留字面) ----

def axis(raw, dz=0.06):
    """bridge.py:55-58 通道原始值 282~1722 -> -1..1, 中值 1002 (G12 实测), 带死区"""
    v = max(-1.0, min(1.0, (raw - 1002) / 720.0))
    if abs(v) < dz:
        return 0.0
    return (v - math.copysign(dz, v)) / (1 - dz)


def pos(raw, n=3):
    """bridge.py:63-65 n 段开关原始值 -> 0..n-1"""
    return min(n - 1, max(0, round((raw - 172) / (1811 - 172) * (n - 1))))


def clamp01(v):
    """bridge.py:68-70 网页摇杆值钳到 -1..1"""
    v = float(v)
    return max(-1.0, min(1.0, v))


class DogFsm:
    """模式状态机: 0趴 1站 2走 3坐(Sit动作后的持续姿态)  [bridge.py:396-399 原注释]
    趴 -> 站/走: StandUp 后等 3s 补 BalanceStand(官方提示: StandUp 后关节可能锁死)
    坐 -> 走: C 拨下(静默, 狗已在低位不发指令) -> C 拨上(StandUp 起立)"""

    def __init__(self, cfg=None, now=None):
        self.cfg = cfg if cfg is not None else config
        t0 = time.monotonic() if now is None else now
        # ---- rc_loop 局部变量(bridge.py:556-564) ----
        self.armed = False            # A 键解锁态(rc_loop:555)
        self.last_clean = t0          # 最近干净帧时刻: 失控保护计时锚点(:556)
        self.last_any = t0            # 最近任意帧(含坏帧)时刻: 只反映链路是否活着(:557)
        self.prev_high = None         # A 键上一帧电平(None=首帧只记录)(:559)
        self.last_gear = 0            # 拨档当前位(0=未同步; 趴下切挡只记账)(:560)
        self.gear_sent = 0            # 已真正发给狗的挡位(趴下不发送, 防步态弹狗)(:561)
        self.stand_prev = True        # C 键上一帧电平(首帧前视为稳态)(:562)
        self.key_high = {}            # B/D 键电平记忆: 拨到高位的边沿才触发动作(:563)
        # ---- Dog 内部(bridge.py:401-407) ----
        self.mode = 0                 # 0趴 1站 2走 3坐
        self.moving = False
        self.cooldown_until = 0.0     # 花样动作播放截止(期间摇杆/新动作被屏蔽)
        self.pending = []             # [(触发时刻, 指令名)]
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

    # ---- 内部: 延迟指令到点发送 ----

    def _drain_pending(self, now):
        """Dog.tick(bridge.py:511-519): 到点的延迟指令(起立后补 BalanceStand)"""
        out = []
        due = [t for t in self.pending if now >= t[0]]
        if due:
            self.pending = [t for t in self.pending if now < t[0]]
            for _, name in due:
                out.append(("submit", name, {}))
        return out

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
        """Dog.set_mode(bridge.py:409-448)"""
        now = time.monotonic() if now is None else now
        out = []
        if m == self.mode:
            return out
        # 模式一变就作废未到点的延迟指令(bridge.py:413-416 原注释): 起立后 3s 内拨 C
        # 趴下时, 迟到的 BalanceStand 会把趴着的狗瞬间弹起(10-17 实测危险)
        self.pending = []
        if self.mode == 3 and m == 0:
            # 坐->趴(bridge.py:417-426 原注释): 狗本来就在低位, 只记状态不发指令
            # (发 StandDown 会从坐姿猛塌)
            self.mode = 0
            self.moving = False
            self.cooldown_until = 0.0
            self.action = None
            out.append(("log", "模式 -> 趴"))
            return out
        out.append(("submit", "StopMove", {}))          # bridge.py:427
        if m == 0:
            out.append(("submit", "StandDown", {}))     # bridge.py:428-429
        elif m == 1:
            if self.mode == 0:
                out.append(("submit", "StandUp", {}))   # bridge.py:431-433
                self.pending.append((now + 3.0, "BalanceStand"))
            else:
                out.append(("submit", "BalanceStand", {}))  # bridge.py:434-435
        elif m == 2:
            if self.mode == 3:
                # 坐着时 C 在上位不算数(bridge.py:436-438 原注释): 拨趴->拨站才起立,
                # 防一坐下就被拉起。注意此刻 StopMove(:427) 已发出, 模式保持 3
                return out
            if self.mode == 0:
                out.append(("submit", "StandUp", {}))   # bridge.py:439-441
                self.pending.append((now + 3.0, "BalanceStand"))
        self.mode = m
        self.moving = False
        self.cooldown_until = 0.0
        self.action = None
        out.append(("log", "模式 -> " + ("趴", "站", "走", "坐")[m]))   # bridge.py:448
        return out

    def damp(self):
        """Dog.damp(bridge.py:450-459 原注释): 应急泄力趴窝(已弃用: 自动失控保护改走
        set_mode(0) 缓降, 函数保留仅供手动)"""
        out = [("submit", "Damp", {})]
        self.mode = 0
        self.moving = False
        self.cooldown_until = 0.0
        self.action = None
        return out

    def set_gear(self, g):
        """Dog.set_gear(bridge.py:461-469 原注释): 三档拨档 = 步态切换(实测本固件
        SpeedLevel 只收 -1/1, 三档速度实为步态): 1=EconomicGait 2=FreeWalk 3=TrotRun"""
        gait = {1: "EconomicGait", 2: "FreeWalk", 3: "TrotRun"}.get(g)
        if gait:
            return [("submit", gait, {}), ("log", "挡位 -> %d (%s)" % (g, gait))]
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
        if self.mode != 2 or self.pending or now < self.cooldown_until:   # bridge.py:477
            return out, "NOTSTAND"
        out.append(("submit", name, {}))
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
        """Dog.move(bridge.py:493-502): 仅走模式且无 pending/冷却时响应"""
        now = time.monotonic() if now is None else now
        out = []
        if self.mode != 2 or self.pending or now < self.cooldown_until:
            return out
        if (vx, vy, vyaw) != (0.0, 0.0, 0.0):
            out.append(("submit", "Move",
                        {"x": round(vx, 3), "y": round(vy, 3), "z": round(vyaw, 3)}))
            self.moving = True
        elif self.moving:
            out.append(("submit", "StopMove", {}))
            self.moving = False
        return out

    def hold(self):
        """Dog.hold(bridge.py:504-509 原注释): 失控保护第一步, 幂等停走"""
        out = []
        if self.moving:
            out.append(("submit", "StopMove", {}))
            self.moving = False
        return out

    # ---- 事件 ----

    def link(self, gen):
        """DogLink 连上狗(state.link_gen += 1, bridge.py:253-257) -> dog.reset()
        (bridge.py:521-539 原注释):
          开机首连: 一条指令都不发 —— 狗开机自己站起来, 打断它(哪怕 StopMove)都是
            干扰; 记 mode=1(站立), 锁定等解锁, 解锁后 C 在上位直接可控(不再补 StandUp)
          断链重连: 只发 StopMove 停走保持姿态(狗可能正按最后杆量盲走, 必须急停),
            mode 保持"""
        out = []
        if gen != self.link_gen_cur:
            self.link_gen_cur = gen
            self.link_up = True
            first = gen <= 1                      # bridge.py:527 state.link_gen <= 1
            if not first:
                out.append(("submit", "StopMove", {}))
            else:
                self.mode = 1
            self.moving = False
            self.cooldown_until = 0.0
            self.pending = []
            self.action = None
            out.append(("log", "开机首连: 狗自行起立, 已锁定等待解锁" if first
                        else "断链重连: 停走保持姿态, 请重新解锁"))    # bridge.py:538-539
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
        self.gear = None
        self.sbus_ok = False
        self.sbus_clean = False
        return []

    def frame(self, ch, fs, lost, source="sbus", now=None):
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
        out = self._drain_pending(now)            # :600 d.tick() —— 必须在帧处理前:
        #                                           set_mode 若清 pending, 迟到指令不得先漏发
        # 载荷输出(bridge.py:601-607 原注释): B键->15脚pwm1两档脉宽(1650分界的
        # RC解码继电器)+IO电平
        lv = ch[self.cfg.CH_RELAY] > self.cfg.RELAY_ON_RAW      # :602
        out += self._set_relay(lv)                # :604-606 set_us(1950/1050) + set_level(lv)
        # A 键: 解锁/上锁(bridge.py:610-636)
        a_high = ch[self.cfg.CH_ARM] > self.cfg.ARM_ON          # :610
        if self.link_gen != self.link_gen_cur:    # :611 (重)连上狗: 回到上锁态, 防重连瞬间狗自行动作
            self.link_gen = self.link_gen_cur     # :612
            if self.armed:
                self.armed = False                # :614
                out += self.hold()                # :616 停走保持姿态, 不趴下(09-29 用户要求)
                out.append(("log", "链路重连, 已回上锁态(保持站立), 请重新解锁"))  # :618
            self.prev_high = a_high               # :619
        if self.prev_high is None:                # :620-621 首帧只记录电平, 不判定解锁边沿
            self.prev_high = a_high
        if self.armed and not a_high:
            # A 拨到上锁位(:622-628): 上锁 + 趴下
            self.armed = False
            out += self.set_mode(0, now=now)      # :625
            out.append(("log", "已上锁(RC)"))     # :628
        elif not self.armed and a_high and not self.prev_high:
            # 翻拨边沿解锁(:629-635 原注释): 只认翻拨动作, A 一直停在解锁位不算
            # (连发/持续高电平不误判 —— prev_high 每帧更新, 平稳高电平无边沿)
            self.armed = True                     # :630
            self.web_armed = False                # :632 RC 接管, 网页控制让位
            out.append(("log", "已解锁(RC)"))     # :635
        self.prev_high = a_high                   # :636
        stand = ch[self.cfg.CH_STAND] > self.cfg.STAND_ON   # :637 C 电平每帧都算(上锁期也跟)
        if self.armed:
            # C 键电平决定站/走或趴(:639-647 原注释): 趴下时摇杆自失效(仅走模式响应摇杆)。
            # 解锁弹起修复(10-14 原注释): 趴着的狗解锁时 C 原本就在上, 不再自动起立,
            # 起立只认 C 下->上翻拨边沿; 站/走状态 C 在上照旧直控(开机解锁即可控不回退)
            if stand and self.stand_prev is False:
                out += self.set_mode(2, now=now)  # :642-643 C 下->上边沿: 正常起立请求
            elif stand and self.mode != 0:
                out += self.set_mode(2, now=now)  # :644-645 C 稳在上且狗本是站/走: 补同步模式(开机首连 mode=1)
            else:
                out += self.set_mode(0, now=now)  # :646-647 C 在下(或趴着): 趴
            g = pos(ch[self.cfg.CH_GEAR], 3) + 1  # :648 三档拨档 -> 挡1/2/3
            if g != self.last_gear:
                self.last_gear = g                # :649-650
                self.gear = g                     # :651 展示镜像
            # 步态只在站立时同步(:652-656 原注释): 趴着切挡(含趴着解锁, 无论 C 在上
            # 在下)只记账, 否则 TrotRun 等步态指令会把趴下的狗直接弹起来(10-10 实测 bug)
            if stand and self.mode != 0 and self.gear_sent != g:
                self.gear_sent = g                # :655
                out += self.set_gear(g)           # :656
            vx_max, vy_max, vyaw_max = self.cfg.GEAR_SPEEDS.get(
                g, (self.cfg.VX_MAX, self.cfg.VY_MAX, self.cfg.VYAW_MAX))  # :657-658
            ax = axis(ch[self.cfg.CH_PITCH]) * self.cfg.SIGN_PITCH   # :659
            ay = axis(ch[self.cfg.CH_ROLL]) * self.cfg.SIGN_ROLL     # :660
            az = axis(ch[self.cfg.CH_YAW]) * self.cfg.SIGN_YAW       # :661
            # B/D 键动作(:662-667 原注释): 拨到高位的边沿触发一次, 摇杆回中时才响应
            # (当前 config.KEY_ACTIONS={} 已退役, 逻辑保留)
            for cid, (name, dur) in self.cfg.KEY_ACTIONS.items():
                high = ch[cid] > 1002             # :664
                if high and not self.key_high.get(cid) and not (ax or ay or az):
                    cmds, _r = self.trick(name, dur, now=now)        # :666
                    out += cmds
                self.key_high[cid] = high         # :667
            out += self.move(ax * vx_max, ay * vy_max, az * vyaw_max, now=now)  # :668
        self.stand_prev = stand   # :669 每帧更新(含上锁期): 解锁瞬间才不会把陈旧电平当边沿
        return out

    def tick(self, now=None):
        """rc_loop 每循环一次(bridge.py:670-671 无帧/坏帧时 d.tick() 的兜底,
        干净帧时 frame() 内已做过, 此处幂等) + 网页摇杆驱动(:700-704)"""
        now = time.monotonic() if now is None else now
        out = self._drain_pending(now)
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
                    out += self.hold()                  # :698 停走即可, 姿态交给狗自己保持(不趴下)
        return out

    def web_cmd(self, cmd, vx=None, vy=None, vyaw=None, now=None):
        """网页 /ws 指令(bridge.py:746-767) + 连接断开(:786-793)。
        cmd: "arm"/"disarm"/"damp"/"stand"/"walk"/"axes"(摇杆值)/"closed"(ws 断开)"""
        now = time.monotonic() if now is None else now
        out = []
        if cmd == "axes":
            # :764-767 原注释: 摇杆消息三键齐全才赋值; 心跳{ping:1}不得清零速度
            if vx is not None:
                self.web_vx = clamp01(vx)
                self.web_vy = clamp01(vy if vy is not None else 0.0)
                self.web_vyaw = clamp01(vyaw if vyaw is not None else 0.0)
            return out
        if self.armed:
            return out                       # :748-750 RC 优先, 网页指令忽略
        if cmd == "arm":
            self.web_armed = True            # :751-753
            if self.mode == 0:
                out += self.set_mode(1, now=now)     # :754-755
        elif cmd in ("disarm", "damp"):
            self.web_armed = False            # :756-758
            out += self.set_mode(0, now=now)          # :759
        elif cmd == "stand":
            out += self.set_mode(1, now=now)          # :760-761
        elif cmd == "walk":
            out += self.set_mode(2, now=now)          # :762-763
        elif cmd == "closed":
            # :786-793 网页端断开, 解除网页控制并趴下(仅 RC 也未解锁时)
            self.web_armed = False
            self.web_vx = self.web_vy = self.web_vyaw = 0.0
            if not self.armed:
                out += self.set_mode(0, now=now)      # :792-793
        return out
