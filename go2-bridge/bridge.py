# -*- coding: utf-8 -*-
"""Go2 SBUS 遥控桥
云卓 G12 SBUS -> 板子解码 -> WebRTC(App 同款通道, MCF 指令) -> 宇树 Go2
浏览器遥控页: http://<板子IP>:<WEB_PORT>  (RC 解锁时 RC 优先, 网页仅 RC 上锁时生效)

设计: 三个线程各管各的, 互不阻塞、各自自愈 ——
  DogLink 线程: WebRTC 会话, 断线/拔网线自动重连(载波+信令口 TCP 看门狗强拆死会话)
  rc_loop 线程: SBUS 串口, 未接/拔出每 3s 重试, 串口重开需重新解锁
  web 线程:     FastAPI + WebSocket, 浏览器关闭即解除网页控制并趴下
板子可以先于狗/接收机上电, 什么都不接也正常启动。
"""
import asyncio
import collections
import json
import math
import random
import signal
import socket
import threading
import time
import os

import config
import journal
from sbus import Sbus
from servo import Servo, GpioOut
from pa import Pa          # P0-1/T2.2 后 main 不再用 Pa, 但测试经 bridge.Pa 构造, 保留导出
import netrc

try:  # v2.x 把类挪进了 webrtc_driver 子模块, 兼容两种导入路径
    from unitree_webrtc_connect.webrtc_driver import (
        UnitreeWebRTCConnection, WebRTCConnectionMethod)
    from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD_MCF
    DOG_LIB = True
except ImportError:
    try:
        from unitree_webrtc_connect import (
            UnitreeWebRTCConnection, WebRTCConnectionMethod)
        from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD_MCF
        DOG_LIB = True
    except ImportError:
        DOG_LIB = False


_LOG_BUF = collections.deque(maxlen=600)   # 内存日志环: /logs 端点给控制台日志面板用
_J_EVERY_N = max(1, int(config.JOURNAL_EVERY_N))   # journal 高频采样比(帧/Move), 防误配 0
_T0 = time.monotonic()    # 进程启动锚点: /state 的 uptime_s 用


def log(*a):
    line = time.strftime("[%H:%M:%S]") + " " + " ".join(str(x) for x in a)
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


def _set_cmd_result(name, result):
    """更新最近一条非 Move 指令的发送结果(供 /state.last_cmd)。"""
    lc = state.last_cmd
    if isinstance(lc, dict) and lc.get("name") == name:
        lc["result"] = result


def _sys_snapshot():
    """板上系统健康: load1 / 内存占用% / CPU 温度; 读不到给 None, 绝不抛。"""
    load1 = None
    try:
        with open("/proc/loadavg") as f:
            load1 = float(f.read().split()[0])
    except Exception:
        pass
    mem_pct = None
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.strip().split()[0])
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        if total:
            mem_pct = round((total - avail) * 100.0 / total, 1)
    except Exception:
        pass
    cpu_temp = None
    try:
        import glob as _glob
        for p in _glob.glob("/sys/class/hwmon/hwmon*/name"):
            try:
                with open(p) as f:
                    if f.read().strip() == "cpu_thermal":
                        with open(p.replace("name", "temp1_input")) as t:
                            cpu_temp = round(int(t.read().strip()) / 1000.0, 1)
                        break
            except OSError:
                continue
        if cpu_temp is None:
            with open("/sys/class/thermal/thermal_zone0/temp") as t:
                cpu_temp = round(int(t.read().strip()) / 1000.0, 1)
    except Exception:
        pass
    return {"load1": load1, "mem_pct": mem_pct, "cpu_temp": cpu_temp}


def axis(raw, dz=0.06):
    """通道原始值 282~1722 -> -1..1, 中值 1002 (G12 实测), 带死区"""
    v = max(-1.0, min(1.0, (raw - 1002) / 720.0))
    if abs(v) < dz:
        return 0.0
    return (v - math.copysign(dz, v)) / (1 - dz)


def pos(raw, n=3):
    """n 段开关原始值 -> 0..n-1"""
    return min(n - 1, max(0, round((raw - 172) / (1811 - 172) * (n - 1))))


def clamp01(v):
    v = float(v)
    return max(-1.0, min(1.0, v))


def _path_ok(tcp=True):
    """板->狗物理链路探测: 网线在位; tcp=True 时再探狗信令口(阻塞式, 供看门狗线程池调用)"""
    try:
        with open("/sys/class/net/%s/carrier" % config.ETH_IFACE) as f:
            if f.read().strip() != "1":
                return False
    except OSError:
        pass                      # 接口不存在/读不到就只靠 TCP 探测
    if not tcp:
        return True
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect((config.ROBOT_IP, config.SIGNAL_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


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
    last_src = "sbus"     # 最近控制帧来源: sbus / netrc
    last_cmd = None       # 最近一条非 Move 指令 {name, ts, result}

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
dog = None             # main() 里赋值; DogLink 连上狗后回调其 reset()


class SbusStats:
    """SBUS 帧统计: rc_loop 经串口回调喂批次, web_server 快照读; 一把锁, 持锁极短。
    帧到达按串口 read 批次观测(同批多帧共享时间戳), 故帧间隔 = 批间隔/本批帧数
    (SBUS 固定周期, 均值即标称间隔; 断流时该值拉大, >25ms 记一次丢帧间隔)。
    接收机自身的 fs/lost 标志是丢帧的另一个独立真值来源。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.total = 0          # 总帧数
        self.fs_cnt = 0         # 失控标志帧数
        self.lost_cnt = 0       # 丢帧标志帧数
        self.gap_cnt = 0        # 帧间隔 >25ms 的次数
        self.last_iv = None     # 最近帧间隔 ms(无数据 None)
        self.fps = 0            # 1s 滑窗帧率
        self.hist = [0] * 60    # 最近 60s 每秒帧数(环形, 最新在 _hist_i)
        self.ch = None          # 最近一帧干净帧的 16 通道原始值
        self._ts = collections.deque()   # 1s 滑窗帧时间戳
        self._t_prev = None     # 上一批到达时刻
        self._sec0 = None       # 当前秒桶
        self._sec_n = 0         # 当前秒已收帧数
        self._hist_i = 59       # 环形最新槽位

    def _roll(self, sec):
        """秒桶翻转: 结算旧秒并补零推进(链路停了也照推, 柱状图不说谎)。须持锁调用"""
        if self._sec0 is None:
            self._sec0 = sec
            return
        if sec == self._sec0:
            return
        self.hist[self._hist_i] = self._sec_n
        for _ in range(min(sec - self._sec0, 60)):
            self._hist_i = (self._hist_i + 1) % 60
            self.hist[self._hist_i] = 0
        self._sec0 = sec
        self._sec_n = 0

    def feed(self, frames, now=None):
        """一批(同一次串口 read 解出的)帧喂入, frames: [(ch, fs, lost), ...]"""
        now = time.monotonic() if now is None else now
        with self.lock:
            for ch, fs, lost in frames:
                self.total += 1
                if fs:
                    self.fs_cnt += 1
                if lost:
                    self.lost_cnt += 1
                if not fs and not lost:
                    self.ch = list(ch)
                self._ts.append(now)
            while self._ts and self._ts[0] <= now - 1.0:
                self._ts.popleft()
            self.fps = len(self._ts)
            self._roll(int(now))
            self._sec_n += len(frames)
            if self._t_prev is not None:
                iv = (now - self._t_prev) * 1000.0 / len(frames)
                self.last_iv = round(iv, 1)
                if iv > 25.0:
                    self.gap_cnt += 1
            self._t_prev = now

    def snapshot(self, now=None):
        """供 /ws 推送: 顺带裁剪滑窗/推进秒桶, 返回可直接 json 化的字段"""
        now = time.monotonic() if now is None else now
        with self.lock:
            while self._ts and self._ts[0] <= now - 1.0:
                self._ts.popleft()
            self.fps = len(self._ts)
            self._roll(int(now))
            hist = self.hist[self._hist_i + 1:] + self.hist[:self._hist_i + 1]
            hist[-1] = self._sec_n      # 最新一秒是进行中的当前秒
            return {"total": self.total, "fs_cnt": self.fs_cnt,
                    "lost_cnt": self.lost_cnt, "gap_cnt": self.gap_cnt,
                    "last_iv": self.last_iv, "fps": self.fps,
                    "hist": hist, "ch": self.ch}


sbus_stats = SbusStats()


class DogLink:
    """WebRTC 会话线程: 永远重连; 队列中连续 Move 只发最新一条"""

    def __init__(self):
        self.loop = None
        self.q = None
        self.conn = None
        self._last_logged = None      # LOG_CMDS 去重: 连续相同的指令只记一条
        self._last_gear_read = None   # 心跳回读的挡位(变化才记日志)
        self._mv_n = 0                # journal 采样计数: Move 指令每 N 条记 1 条
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        if not DOG_LIB:
            state.link = "lib_missing"
            log("unitree_webrtc_connect 未安装, 连狗功能停用 (bash install.sh)")
            return
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.q = asyncio.Queue()
        self.loop.run_until_complete(self._session())

    async def _session(self):
        kw = {}
        if config.METHOD == "LocalSTA":
            kw["ip"] = config.ROBOT_IP
        if config.AES_KEY:
            kw["aes_128_key"] = config.AES_KEY
        while True:
            state.link = "connecting"
            try:
                # 物理链路恢复前不发起连接, 防半开连接把 connect 挂死
                n = 0
                while not await self._probe():
                    state.link = "down"
                    if n % 10 == 0:
                        log("到狗链路不通(网线/狗未开机), 等待恢复...")
                    n += 1
                    await asyncio.sleep(2)
                self.conn = UnitreeWebRTCConnection(
                    getattr(WebRTCConnectionMethod, config.METHOD), **kw)
                try:
                    await asyncio.wait_for(self.conn.connect(), 20)
                except asyncio.TimeoutError:
                    raise RuntimeError("connect 20s 超时")
                if hasattr(self.conn.datachannel, 'wait_for_connection'):
                    await self.conn.datachannel.wait_for_connection()  # v1.x API, v2 已内置验证
                state.link = "up"
                state.link_gen += 1
                log("WebRTC 已连接")
                self._subscribe_dog_state()   # 先订阅真实姿态, 再决定 reset 的本地 mode
                await self._wait_dog_state()
                if dog is not None:
                    dog.reset()          # (重)连上后狗侧实际状态未知, 首连按狗上报高度定档
                while not self.q.empty():
                    self.q.get_nowait()  # 丢弃重连瞬间残留的旧指令
                await self._guarded_consume()
            except Exception as e:
                state.link = "down"
                log("WebRTC 会话结束, 3s 后重连:", repr(e))
            self._close_conn()
            await asyncio.sleep(3)

    async def _guarded_consume(self):
        """_consume/看门狗/心跳 三方赛跑: 拔网线靠看门狗, 会话半死靠心跳, 谁先出事都强拆重连"""
        worker = asyncio.ensure_future(self._consume())
        watch = asyncio.ensure_future(self._watch_link())
        beat = asyncio.ensure_future(self._heartbeat())
        done, _ = await asyncio.wait({worker, watch, beat},
                                     return_when=asyncio.FIRST_COMPLETED)
        for t in (worker, watch, beat):
            t.cancel()
        await asyncio.gather(worker, watch, beat, return_exceptions=True)
        if watch in done:
            raise RuntimeError("看门狗: 到狗链路断开")
        if beat in done:
            raise RuntimeError("心跳: 狗应用层无响应, 会话半死")
        raise RuntimeError("接收循环退出: %r" % worker.exception())

    async def _watch_link(self):
        """每秒看载波(拔线秒级发现), 每3秒才探一次狗信令口(免得一天打狗8.6万次连接)"""
        n = 0
        while True:
            await asyncio.sleep(1.0)
            n += 1
            if not await self._probe(n % 3 == 0):
                return

    async def _heartbeat(self):
        """心跳回读: Move 是 noreply 流, 狗侧会话半死(信令活着/指令黑洞)时它毫无察觉,
        这里周期发有回包的 GetSpeedLevel 探狗应用层, 连续 HEARTBEAT_MISS 次等不到
        回包就返回触发拆会话重连。任何回包(含拒收 code)都算狗活着——半死的特征是
        彻底无回音。回读的挡位变化顺手记日志"""
        miss = 0
        while True:
            await asyncio.sleep(config.HEARTBEAT_S)
            try:
                r = await asyncio.wait_for(
                    self.conn.datachannel.pub_sub.publish_request_new(
                        RTC_TOPIC["SPORT_MOD"],
                        {"api_id": SPORT_CMD_MCF["GetSpeedLevel"],
                         "parameter": json.dumps({})}),
                    config.HEARTBEAT_TIMEOUT)
                miss = 0
                data = r.get("data", {}).get("data")
                if data != self._last_gear_read:
                    self._last_gear_read = data
                    log("心跳回读挡位:", data)
            except asyncio.TimeoutError:
                miss += 1
                log("心跳无回包 %d/%d" % (miss, config.HEARTBEAT_MISS))
                if miss >= config.HEARTBEAT_MISS:
                    log("狗应用层连续无响应, 判会话半死, 强制重连")
                    return
            except Exception as e:
                miss += 1     # 发送本身异常(重连窗口期常见)同样计一次失联
                log("心跳发送异常 %d/%d:" % (miss, config.HEARTBEAT_MISS), repr(e)[:80])
                if miss >= config.HEARTBEAT_MISS:
                    return

    async def _probe(self, tcp=True):
        return await asyncio.get_running_loop().run_in_executor(None, _path_ok, tcp)

    def _subscribe_dog_state(self):
        """订阅狗的真实姿态(10-20): rt/sportmodestate 周期上报 mode/body_height/
        gait_type/velocity。首连用它决定本地 mode, 起立用它判"起完了没有",
        监控台也直接显示狗自报值 —— 不再用本地假想状态。"""
        try:
            for key in ("SPORT_MOD_STATE", "LF_SPORT_MOD_STATE"):
                topic = RTC_TOPIC.get(key)
                if not topic:
                    continue
                self.conn.datachannel.pub_sub.subscribe(topic, self._on_sport_state)
                log("已订阅狗姿态:", topic)
        except Exception as e:
            log("订阅狗姿态失败(起立判定回落超时兜底):", repr(e))

    def _on_sport_state(self, msg):
        """回调跑在 asyncio 线程: 只存快照(单属性赋值), 不碰控制路径。"""
        d = msg.get("data") if isinstance(msg, dict) else None
        if not isinstance(d, dict):
            return
        try:
            state.dog_mode = d.get("mode")
            h = d.get("body_height")
            state.dog_height = float(h) if isinstance(h, (int, float)) else None
            state.dog_gait = d.get("gait_type")
            v = d.get("velocity")
            state.dog_vel = ([float(v[0]), float(v[1]), float(v[2])]
                             if isinstance(v, (list, tuple)) and len(v) >= 3 else None)
            state.dog_state_t = time.monotonic()
            state.dog_state_n += 1
            if state.dog_state_n == 1:
                log("狗姿态首包:", str(d)[:300])
        except Exception:
            pass

    async def _wait_dog_state(self):
        """首连等第一条新鲜姿态(上限 DOG_STATE_WAIT_S), 让 dog.reset() 按真实高度定档。"""
        fresh = (state.dog_state_t is not None
                 and time.monotonic() - state.dog_state_t < config.DOG_STATE_STALE_S)
        if fresh:
            return
        t0 = time.monotonic()
        while time.monotonic() - t0 < config.DOG_STATE_WAIT_S:
            if state.dog_state_t is not None:
                return
            await asyncio.sleep(0.1)
        log("首连 %.0fs 内未收到狗姿态, 按旧逻辑假定站立" % config.DOG_STATE_WAIT_S)

    def _close_conn(self):
        c, self.conn = self.conn, None
        if c is None:
            return
        try:
            r = c.close()            # 库版本不同 close 可能是同步/异步, 都兼容
            if hasattr(r, "__await__"):
                fut = asyncio.ensure_future(r)
                fut.add_done_callback(lambda t: t.cancelled() or t.exception())
        except Exception:
            pass

    async def _consume(self):
        while True:
            batch = [await self.q.get()]
            while not self.q.empty():
                batch.append(self.q.get_nowait())
            moves = [b for b in batch if b[0] == "Move"]
            for b in batch:              # 模式类指令按序全发
                if b[0] != "Move":
                    await self._send(*b)
            if moves:
                await self._send(*moves[-1])

    async def _send(self, name, p):
        if name == "Move":
            # Move 是高频流控指令, 官方示例走 noreply 免等回复
            rid = int(time.time() * 1000) % 2147483648 + random.randint(0, 1000)
            self.conn.datachannel.pub_sub.publish_without_callback(
                RTC_TOPIC["SPORT_MOD"],
                {"header": {"identity": {"id": rid, "api_id": SPORT_CMD_MCF["Move"]},
                            "policy": {"priority": 0, "noreply": True}},
                 "parameter": json.dumps(p), "binary": []})
            return
        try:
            r = await asyncio.wait_for(
                self.conn.datachannel.pub_sub.publish_request_new(
                    RTC_TOPIC["SPORT_MOD"],
                    {"api_id": SPORT_CMD_MCF[name], "parameter": json.dumps(p)}),
                config.REQ_TIMEOUT_S)   # 无回包上限: 防狗半死时单条请求冻死整条指令管线
            code = r.get("data", {}).get("header", {}).get("status", {}).get("code")
            if code not in (0, None, 200):
                log(name, "被狗拒绝 code =", code)
                _set_cmd_result(name, "rejected:%s" % code)
            else:
                _set_cmd_result(name, "ok")
                if name == "GetSpeedLevel":
                    log("狗侧回读挡位:", r.get("data", {}).get("data"))
        except asyncio.TimeoutError:
            log("发送超时(狗 %ss 无回包):" % config.REQ_TIMEOUT_S, name)
            _set_cmd_result(name, "timeout")
        except Exception as e:
            log("发送失败:", name, repr(e))
            _set_cmd_result(name, "error")

    def submit(self, name, **p):
        # journal 埋点(kind=cmd): 非 Move 全记(低频); Move 是 20Hz 流, 每 N 条采 1 条
        if name == "Move":
            self._mv_n += 1
            if self._mv_n % _J_EVERY_N == 0:
                journal.record("cmd", name=name, **p)
        else:
            journal.record("cmd", name=name, **p)
            state.last_cmd = {"name": name, "ts": time.time(), "result": "pending"}
        if self.loop and state.link == "up":
            # Move 是 20Hz 连发流, 不进 LOG_CMDS(微抖值每条都不同, 会变刷屏+刷TF卡)
            if config.LOG_CMDS and name != "Move" and (name, p) != self._last_logged:
                self._last_logged = (name, p)
                log("指令 ->", name, p if p else "")
            try:
                self.loop.call_soon_threadsafe(self.q.put_nowait, (name, p))
            except RuntimeError:
                pass   # 重连窗口期事件循环已收摊, 丢弃(断链下 submit 本就不该被调)


class Dog:
    """模式状态机: 0趴 1站 2走 3坐(Sit动作后的持续姿态)
    趴 -> 站/走: StandUp 后等 3s 补 BalanceStand(官方提示: StandUp 后关节可能锁死)
    坐 -> 走: C 拨下(静默, 狗已在低位不发指令) -> C 拨上(StandUp 起立)
    安全互锁(10-20 供电口事故止血): now 传入时基锚点(部署入口 main 传进程启动时刻)
    才启用互锁+起立保护期; 缺省 None = 单元测试直构, 保持无互锁的旧行为"""

    def __init__(self, link, now=None):
        self.link = link
        self.lock = threading.Lock()
        self.mode = 0
        self.moving = False
        self.cooldown = 0.0  # 花样动作播放截止时刻(期间摇杆/新动作被屏蔽)
        self.pending = []    # [(monotonic 触发时刻, 指令名)]
        self._t0 = now             # 时基锚点(进程启动时刻): 起立保护期 + 互锁总开关
        self._last_big = None      # 上一次大动作(StandUp/StandDown)执行时刻: 互锁计时
        # 状态驱动起立(10-20): StandUp 后等狗上报 body_height 稳定再补 BalanceStand,
        # 不再写死 3 秒(第一次起立比后续慢, 固定 3 秒会插进起立过程把狗弹起来)
        self.rising = False        # 起立流程中(已发 StandUp, 等高度稳定)
        self.rise_t = 0.0          # 本次 StandUp 发出时刻
        self.rise_deadline = 0.0   # 超时兜底时刻
        self._h_hist = []          # [(monotonic, body_height)] 最近 0.6s 高度样本

    def set_mode(self, m, now=None):
        """请求切模式。返回 True=已执行或本来就该状态; False=被互锁/起立保护期/
        坐姿规则拦下(未发指令, 状态未动), rc_loop 的每帧重试到点兜住"""
        now = time.monotonic() if now is None else now
        with self.lock:
            if m == self.mode:
                return True
            # 大动作 = 趴0 <-> 站/走1|2 之间切换(会发 StandUp 或 StandDown);
            # 坐3 本在低位: 3->0 静默 / 3->1 走 BalanceStand, 都不算大动作
            big = (m == 0) != (self.mode == 0)
            # 互锁/保护期只拦"起立"(m!=0): 上锁/趴下任何时候都必须放行 ——
            # 否则 A 键上锁会被静默吞掉, 狗保持最后速度, 收权失效(10-20 复核抓出)
            if big and m != 0 and self._t0 is not None:
                if m != 0 and now - self._t0 < config.STAND_GUARD_S:
                    # 起立保护期(10-20): 开机初期供电口未稳, 拒绝任何会发 StandUp 的请求
                    journal.record("event", ev="stand_guard", to=m,
                                   remain=round(self._t0 + config.STAND_GUARD_S - now, 3))
                    _ev("safety", "stand_guard", "to=%s" % m)
                    log("起立保护期: 拒绝起立(开机 %.0fs 内), %.1fs 后到点自动重试"
                        % (config.STAND_GUARD_S,
                           self._t0 + config.STAND_GUARD_S - now))
                    return False
                if (self._last_big is not None
                        and now - self._last_big < config.MODE_DWELL_S):
                    # 大动作互锁(10-20): 两次起立/趴下太近会触发狗供电口保护性断电
                    journal.record("event", ev="interlock", to=m,
                                   remain=round(self._last_big + config.MODE_DWELL_S - now, 3))
                    _ev("safety", "interlock", "to=%s" % m)
                    log("互锁: 距上一次起立/趴下 %.2fs < %.1fs, 拒绝(%.1fs 后到点自动重试)"
                        % (now - self._last_big, config.MODE_DWELL_S,
                           self._last_big + config.MODE_DWELL_S - now))
                    return False
            prev_mode = self.mode     # journal 用: 切换前旧模式
            # 模式一变就作废未到点的延迟指令与起立流程: 起立过程中拨 C 趴下时,
            # 迟到的 BalanceStand 会把趴着的狗瞬间弹起(10-17 实测危险)
            self.pending = []
            self.rising = False
            self._h_hist = []
            if self.mode == 3 and m == 0:
                # 坐->趴: 狗本来就在低位, 只记状态不发指令(发 StandDown 会从坐姿猛塌)
                self.mode = 0
                self.moving = False
                self.cooldown = 0.0
                state.mode = 0
                state.action = None
                journal.record("event", ev="mode", prev=prev_mode, to=0)
                log("模式 -> 趴")
                return True
            self.link.submit("StopMove")
            if m == 0:
                self.link.submit("StandDown")
            elif m == 1:
                if self.mode == 0:
                    self._begin_rise(now)
                else:
                    self.link.submit("BalanceStand")
            elif m == 2:
                if self.mode == 3:
                    return False  # 坐着时 C 在上位不算数: 拨趴->拨站才起立, 防一坐下就被拉起
                if self.mode == 0:
                    self._begin_rise(now)
            self.mode = m
            self.moving = False
            self.cooldown = 0.0
            state.mode = m
            state.action = None
            journal.record("event", ev="mode", prev=prev_mode, to=m)
            _ev("mode", "mode", "%s→%s" % (prev_mode, m))
            log("模式 ->", ("趴", "站", "走", "坐")[m])
            if big and self._t0 is not None:
                self._last_big = now
            return True

    def _begin_rise(self, now):
        """趴->站/走: 发 StandUp, 进入"等高度稳定"状态(调用方持锁)。
        起立期间 move() 被 rising 挡住, 摇杆不会插进起立过程。"""
        self.link.submit("StandUp")
        self.rising = True
        self.rise_t = now
        self.rise_deadline = now + config.STAND_TIMEOUT_S
        self._h_hist = []
        journal.record("event", ev="stand_up", from_mode=self.mode)
        _ev("dog", "stand_up", "from=%s" % self.mode)

    def _finish_rise(self, now, why, h):
        """起立完成(调用方持锁): 补 BalanceStand 并解除 rising 锁。"""
        self.link.submit("BalanceStand")
        self.rising = False
        self._h_hist = []
        journal.record("event", ev="balance", why=why,
                       dt=round(now - self.rise_t, 2), height=h)
        _ev("dog", "balance", "%s %.2fs" % (why, now - self.rise_t))
        log("起立判定(%s): %.2fs 后补 BalanceStand, 狗自报高度=%s"
            % (why, now - self.rise_t, ("%.3fm" % h) if h is not None else "n/a"))

    def _maybe_balance(self, now):
        """状态驱动起立完成判定(调用方持锁): 狗自报 body_height 在 0.6s 窗口内
        稳定且高于站姿阈值 -> 起立完成; 无姿态数据/一直不稳 -> 超时兜底。
        这就是"第一次起立慢、后续快"都安全的根本原因: 不看固定秒数, 看狗自己起完没。"""
        if not self.rising:
            return
        h, ts = state.dog_height, state.dog_state_t
        fresh = h is not None and ts is not None and now - ts < config.DOG_STATE_STALE_S
        if not fresh and now - self.rise_t >= 3.0:
            # 无姿态数据: 回落旧行为(固定 3 秒), 与拆分前完全一致
            self._finish_rise(now, "no_telemetry", None)
            return
        if fresh:
            self._h_hist.append((now, h))
            self._h_hist = [(t, v) for t, v in self._h_hist if now - t <= 0.6]
            if (len(self._h_hist) >= 3 and h > config.DOG_STAND_HEIGHT
                    and max(v for _, v in self._h_hist)
                    - min(v for _, v in self._h_hist) < config.DOG_HEIGHT_STABLE):
                self._finish_rise(now, "stable", h)
                return
        if now >= self.rise_deadline:
            self._finish_rise(now, "timeout", h)

    def damp(self):
        """应急泄力趴窝(已弃用: 自动失控保护改走 set_mode(0) 缓降, 函数保留仅供手动)"""
        with self.lock:
            self.link.submit("Damp")
            journal.record("event", ev="damp")
            self.mode = 0
            self.moving = False
            self.cooldown = 0.0
            state.mode = 0
            state.action = None

    def set_gear(self, g):
        """三档拨档 = 步态切换(实测本固件 SpeedLevel 只收 -1/1, 三档速度实为步态):
        1=EconomicGait经济 2=FreeWalk自由 3=TrotRun奔跑"""
        with self.lock:
            gait = {1: "EconomicGait", 2: "FreeWalk", 3: "TrotRun"}.get(g)
            if gait:
                self.link.submit(gait)
                journal.record("event", ev="gear", g=g, gait=gait)
                log("挡位 ->", g, "(%s)" % gait)

    def trick(self, name, dur):
        """花样动作: 仅走模式(站立)可触发; 播放期(冷却)内摇杆/新动作被屏蔽
        返回 True=已触发, False=条件不满足(未解锁/非行走/动作占用中)"""
        with self.lock:
            now = time.monotonic()
            # 链路断时 submit 会被静默丢弃, 不能只收冷却不认账(否则谎报成功)
            if state.link != "up" or self.mode != 2 or self.pending or now < self.cooldown:
                return False
            self.link.submit(name)
            self.cooldown = now + dur
            state.action = name        # 镜像给网页端显示动作锁(只动 state, 不碰统计锁)
            state.action_end = now + dur
            journal.record("event", ev="trick", name=name, dur=dur)
            log("动作 ->", name, "(%ds)" % dur)
            if name == "Sit":
                # 坐下是持续姿态: 记成独立状态, C 拨下(静默)->拨上才会再起立
                self.mode = 3
                state.mode = 3
                journal.record("event", ev="mode", prev=2, to=3)
                log("模式 -> 坐")
            return True

    def move(self, vx, vy, vyaw):
        with self.lock:
            # rising: 起立流程未完成(还等 BalanceStand)时摇杆一律不发, 防插进起立过程
            if self.mode != 2 or self.rising or self.pending or time.monotonic() < self.cooldown:
                return
            if (vx, vy, vyaw) != (0.0, 0.0, 0.0):
                self.link.submit("Move", x=round(vx, 3), y=round(vy, 3), z=round(vyaw, 3))
                self.moving = True
            elif self.moving:
                self.link.submit("StopMove")
                self.moving = False

    def hold(self):
        """失控保护第一步: 幂等停走"""
        with self.lock:
            if self.moving:
                self.link.submit("StopMove")
                self.moving = False

    def tick(self):
        """每帧节拍: 状态驱动起立判定 + 到点的延迟指令。"""
        with self.lock:
            now = time.monotonic()
            self._maybe_balance(now)
            due = [t for t in self.pending if now >= t[0]]
            if due:
                self.pending = [t for t in self.pending if now < t[0]]
                for _, name in due:
                    self.link.submit(name)

    def reset(self):
        """WebRTC (重)连上后调用(09-30 用户定案开机行为):
        开机首连: 一条指令都不发 —— 狗开机自己站起来, 打断它(哪怕 StopMove)都是干扰;
                  本地 mode 按狗上报的 body_height 定(10-20: 不再拍脑袋假定站立),
                  锁定等解锁, 解锁后 C 在上位直接可控(不再补 StandUp)
        断链重连: 只发 StopMove 停走保持姿态(狗可能正按最后杆量盲走, 必须急停), mode 保持"""
        with self.lock:
            first = state.link_gen <= 1
            if not first:
                self.link.submit("StopMove")
            else:
                h, ts = state.dog_height, state.dog_state_t
                if (h is not None and ts is not None
                        and time.monotonic() - ts < config.DOG_STATE_STALE_S):
                    self.mode = 1 if h > config.DOG_STAND_HEIGHT else 0
                    journal.record("event", ev="dog_state_sync", height=round(h, 3),
                                   mode=self.mode)
                    _ev("dog", "state_sync", "height=%.3f mode=%s" % (h, self.mode))
                    log("首连读到狗真实高度 %.3fm -> 本地 mode=%d(%s)"
                        % (h, self.mode, "站立" if self.mode else "趴下"))
                else:
                    self.mode = 1
                    log("首连无姿态数据, 按旧逻辑假定站立")
            self.moving = False
            self.cooldown = 0.0
            self.pending = []
            self.rising = False
            self._h_hist = []
            state.mode = self.mode
            state.action = None
            journal.record("event", ev="reset", first=first)
            log("开机首连: 已锁定等待解锁(不发任何指令)" if first
                else "断链重连: 停走保持姿态, 请重新解锁")


# ---- FSM 影子模式(P0-3 / T1.3): 旧逻辑照跑, core/fsm.py 的 DogFsm 并行对照, 只比不发 ----

_SHADOW = None        # FsmShadow 单例; None = 影子关闭(FSM_SHADOW=False 或 core/ 未部署)


def _collapse(cmds):
    """相邻去重: 旧侧四路 GPIO 同电平各记一条, FSM 只出一条; 归一成可比序列"""
    out = []
    for c in cmds:
        if not out or out[-1] != c:
            out.append(c)
    return out


class _RecLink:
    """只读透传包装: 记录 rc_loop 线程窗口内旧路径实际下发的狗指令。
    其他线程(网页/断链复位)的提交不在帧窗口内不记账 —— 其 FSM 对应事件只同步
    状态、输出不参与比较(两边口径一致, 见 FsmShadow 类注释)。"""

    def __init__(self, inner, thread):
        self.inner = inner
        self._thread = thread
        self.cmds = []

    def submit(self, name, **p):
        if threading.current_thread() is self._thread:
            self.cmds.append((name, dict(p)))
        return self.inner.submit(name, **p)

    def take(self):
        out = self.cmds[:]
        self.cmds.clear()
        return out


class _RecRelay:
    """只读透传包装: 按执行层去重语义(servo.py set_us 变化 <25us 不写盘 / 电平
    不变不写盘)记录旧路径请求的继电器目标变化。种子与 DogFsm._relay_us/_gpio_lv
    一致(us=None 首次必记 / lv=False 上电低); 退化硬件(本机无 sysfs)同样按工作板
    记账, 保证影子两侧可比。"""

    def __init__(self, inner):
        self.inner = inner
        self._us = None
        self._lv = False
        self.relay = []
        self.gpio = []

    @property
    def cur_us(self):          # _relay_journal 写前快照透传
        return self.inner.cur_us

    @property
    def level(self):
        return self.inner.level

    @property
    def gpio_num(self):
        return self.inner.gpio_num

    def set_us(self, us):
        if self._us is None or abs(us - self._us) >= 25.0:
            self._us = us
            self.relay.append(("relay", us))
        return self.inner.set_us(us)

    def set_level(self, on):
        on = bool(on)
        if self._lv != on:
            self._lv = on
            self.gpio.append(("gpio", on))
        return self.inner.set_level(on)

    def take(self):
        out = self.relay + self.gpio
        self.relay.clear()
        self.gpio.clear()
        return out


class FsmShadow:
    """DogFsm 影子驱动: 旧逻辑照跑, FSM 并行吃同样事件, 逐帧对照指令序列。
    影子输出绝不落地(不 submit / 不写 sysfs; ("log",...) 也不打 —— 控制台输出
    零变化), 只有差异才记 "FSM 差异" 日志 + journal(fsm_divergence)。
    事件喂法(接线层契约, 见 core/fsm.py 头注释):
      rc_loop 线程 frame/tick/safety/serial_reopened —— 输出进本帧比较窗口;
      跨线程 link(轮询 state)/web_cmd/trick —— 只同步 FSM 状态, 输出丢弃
      (旧侧对应指令在其他线程发出, 不在 rc_loop 帧窗口内, 无从对比)。"""

    def __init__(self, d):
        from core.fsm import DogFsm   # 惰性导入: 不部署 core/ 时 bridge 仍能启动(影子自停)
        self.lock = threading.Lock()
        self.fsm = DogFsm()
        # 启动快照: rc_loop 可能晚于 DogLink 首连(或测试直接预置 mode), 把已发生的抄进来
        self.fsm.mode = d.mode
        # 安全互锁锚点(10-20): 与旧路径同开同关 —— 测试直构 Dog 未传 now 时两侧一并
        # 停用互锁/保护期, 否则单侧生效会制造假影子差异(旧侧放行新侧拦截)
        self.fsm._t0 = d._t0
        self.fsm._last_big = d._last_big
        self.fsm.link_gen = self.fsm.link_gen_cur = state.link_gen
        self.fsm.link_up = (state.link == "up")
        self._last_link = state.link
        self._out = []
        self._rl = None               # 旧侧狗指令记录器
        self._rr = []                 # 旧侧继电器记录器([sv1] + svs)

    def wrap_link(self, inner):
        self._rl = _RecLink(inner, threading.current_thread())
        return self._rl

    def wrap_relay(self, sv1, svs):
        self._rr = [_RecRelay(s) for s in [sv1] + list(svs)]
        return self._rr[0], self._rr[1:]

    # ---- 跨线程事件: 只同步状态, 输出丢弃 ----

    def link_poll(self):
        with self.lock:
            if state.link_gen != self.fsm.link_gen_cur:
                self.fsm.link(state.link_gen, height=state.dog_height,
                              height_t=state.dog_state_t)   # 与 dog.reset() 同口径
            if state.link != self._last_link:
                self._last_link = state.link
                self.fsm.set_link(state.link == "up")

    def web_cmd(self, cmd, vx=None, vy=None, vyaw=None):
        with self.lock:
            self.fsm.web_cmd(cmd, vx=vx, vy=vy, vyaw=vyaw)

    def trick(self, name, dur):
        with self.lock:
            self.fsm.set_link(state.link == "up")  # 与 _trick_gate 同刻的链路口径
            self.fsm.trick(name, dur)

    # ---- rc_loop 线程事件: 输出进本帧比较窗口 ----

    def frame(self, ch, fs, lost, source, now):
        with self.lock:
            self._out += self.fsm.frame(ch, fs, lost, source=source, now=now,
                                        height=state.dog_height,
                                        height_t=state.dog_state_t)

    def tick(self, now):
        # 干净帧的 drain 已在 frame() 内做过(此处传同值 now 幂等), 兼网页摇杆驱动;
        # 无帧/坏帧时传当下时刻 —— 与旧路径 d.tick() 的调用时刻对齐
        with self.lock:
            self._out += self.fsm.tick(now=now, height=state.dog_height,
                                       height_t=state.dog_state_t)

    def safety(self, gap_clean, now):
        with self.lock:
            self._out += self.fsm.safety(gap_clean, now=now)

    def serial_reopened(self):
        with self.lock:
            self.fsm.serial_reopened()

    def serial_offline(self):
        with self.lock:
            self.fsm.serial_offline()

    def compare(self, f, nf, gap_clean):
        """帧尾对照: 旧侧(rc_loop 线程提交 + 继电器目标) vs FSM 本帧输出。
        指令/继电器两类各自按序比(执行器相互独立, 类间顺序无语义); 四路 GPIO
        相邻同值归一成一条再比。"""
        with self.lock:
            fsm_out, self._out = self._out, []
            fsm_sub = [c for c in fsm_out if c[0] == "submit"]
            fsm_rg = _collapse([c for c in fsm_out if c[0] in ("relay", "gpio")])
            old_sub = [("submit", n, p)
                       for n, p in (self._rl.take() if self._rl else [])]
            old_rg = _collapse([c for r in self._rr for c in r.take()])
            snap = {"mode": self.fsm.mode, "armed": self.fsm.armed,
                    "gear_sent": self.fsm.gear_sent, "moving": self.fsm.moving}
        if fsm_sub == old_sub and fsm_rg == old_rg:
            return
        ch, fs, lost = f if f is not None else (None, None, None)
        journal.record("fsm_divergence",
                       src=("netrc" if nf is not None else "sbus") if f is not None else None,
                       fs=fs, lost=lost, gap=round(gap_clean, 3),
                       ch=list(ch) if ch else None,
                       old_sub=old_sub, fsm_sub=fsm_sub,
                       old_relay=old_rg, fsm_relay=fsm_rg, fsm=snap)
        log("FSM 差异: 旧=%s 新=%s (fsm=%s)" % (old_sub + old_rg, fsm_sub + fsm_rg, snap))


def _shadow_safe(meth):
    """影子保险丝: 任一喂点异常即整体停用影子并只记一次日志 —— 影子自身的问题
    绝不波及旧路径(对齐 journal.py 的静默纪律, 但留一行线索)"""
    def wrap(self, *a, **kw):
        global _SHADOW
        try:
            return meth(self, *a, **kw)
        except Exception as e:
            if _SHADOW is self:
                _SHADOW = None
                log("FSM 影子异常, 已停用影子(旧路径不受影响):", repr(e))
    return wrap


for _m in ("link_poll", "web_cmd", "trick", "frame", "tick", "safety",
           "serial_reopened", "serial_offline", "compare"):
    setattr(FsmShadow, _m, _shadow_safe(getattr(FsmShadow, _m)))


def _relay_journal(sv1, svs, was_us, was_lv):
    """journal 埋点(kind=relay): 只记实际写盘成功的 PWM 目标脉宽 / GPIO 电平翻转。
    was_us/was_lv 为写入前快照(sv1.cur_us / [s.level]); servo/GpioOut 内部只在
    写 sysfs 成功后才更新这两个属性, 故"现值 != 快照"即等价于"本次真实写过"。"""
    if sv1.cur_us is not None and sv1.cur_us != was_us:
        journal.record("relay", out="pwm1", us=round(sv1.cur_us))
    for s, was in zip(svs, was_lv):
        if s.level != was:
            journal.record("relay", out="gpio", gpio=s.gpio_num, level=bool(s.level))


def rc_loop(d, pa):
    global _SHADOW
    if config.FSM_SHADOW and _SHADOW is None:
        try:
            _SHADOW = FsmShadow(d)   # main() 已建则复用; 测试直跑 rc_loop 时在此创建
        except ImportError:
            log("core/fsm.py 不可用, FSM 影子模式停用(部署清单需补 core/, 旧路径不受影响)")
    sbus = Sbus(config.SBUS_PORT, on_frames=sbus_stats.feed)
    armed = False
    last_clean = time.monotonic()   # 最近干净帧时刻: 失控保护计时锚点(hold/趴下都看它)
    last_any = time.monotonic()     # 最近任意帧(含坏帧)时刻: 只反映链路是否活着
    link_gen = 0          # WebRTC 连接代数: 每次重连都强制重新解锁
    prev_high = None      # A 键上一帧电平(None=首帧只记录), 解锁只认上锁->解锁的翻拨边沿
    last_gear = 0         # 拨档当前位(0=未同步; 趴下切挡只记账, 起立时补同步)
    gear_sent = 0         # 已真正发给狗的挡位(趴下时拨档变化不发送, 防步态指令把狗弹起来)
    stand_prev = True     # C 键上一帧电平(True=首帧前视为稳态, 防把开机电平当边沿)
    stand_req = False     # C 上沿起立请求: 被 set_mode 拦下(互锁/保护期)时保留, 每帧重试
    lie_req = False       # C 下沿趴下请求: 同上(趴下无保护期)
    last_arm_edge = None  # A 解锁上升沿时刻(去抖锚点; 上锁下降沿永不去抖)
    key_high = {}         # B 键电平记忆: 拨到高位的边沿才触发动作
    jf_n = 0              # journal 帧计数(每 N 帧采 1 帧; 坏帧/A键边沿帧不采样, 必记)
    svs = [GpioOut(g, log) for g in config.RELAY_GPIOS]   # 38/40/37/35脚: IO电平
    sv1 = Servo(config.SERVO_CHIP, config.SERVO1_CH,      # 15脚(PI11)=PWM1: 飞控继电器
                config.SERVO_MIN_US, config.SERVO_MAX_US, log)
    if _SHADOW is not None:              # 影子: 旧路径硬件/指令出口套只读透传记录器(语义不变)
        sv1, svs = _SHADOW.wrap_relay(sv1, svs)
        d.link = _SHADOW.wrap_link(d.link)
    while True:
        if not sbus.opened:
            if sbus.open():
                log("SBUS 串口已打开:", config.SBUS_PORT)
                armed = False               # 串口重开必须重新解锁, 防插拔误触发
                state.rc_armed = False
                state.gear = None
                prev_high = None
                stand_req = False           # 全新会话: 撤销挂起的姿态请求, 防解锁后自动执行
                lie_req = False
                if _SHADOW is not None:
                    _SHADOW.serial_reopened()
            else:
                state.sbus_ok = False
                state.sbus_clean = False
                if _SHADOW is not None:
                    _SHADOW.serial_offline()   # FSM 镜像同步掉线(否则切 live 后 /state 误报链路正常)
                time.sleep(3)
                continue
        t0 = time.monotonic()      # 节拍锚点: read 超时计入周期, 免得 20ms+50ms 串联变 14Hz
        f = sbus.read()
        nf = netrc.get()
        if nf is not None:
            f = (nf, False, False)   # 网络遥控新鲜(300ms)则优先; 通道语义与 SBUS 一致, 下游全复用
        state.last_src = "netrc" if nf is not None else "sbus"
        now = time.monotonic()
        if _SHADOW is not None:
            _SHADOW.link_poll()            # 影子: (重)连/set_link 事件按 state 轮询补喂
        bad = False
        j_must = False                # journal: 本帧必记(坏帧 / A 键解锁上锁边沿帧)
        if f is not None:
            ch, fs, lost = f
            if _SHADOW is not None:
                _SHADOW.frame(ch, fs, lost, "netrc" if nf is not None else "sbus", now)
            state.last_frame_t = now     # /state 镜像: 最近任意帧(含坏帧/网络源)时刻, frame_age 用
            if fs or lost:                  # 坏帧: 链路活着但数据脏, 不作控制输入
                bad = True
                j_must = True
                last_any = now
                if armed:
                    d.hold()                # 立即站定等待恢复(幂等); 不上锁不趴下
            else:
                last_clean = now
                last_any = now
                d.tick()
                # 载荷输出(10-17): B键->15脚pwm1两档脉宽(1650分界的RC解码继电器)+IO电平
                lv = ch[config.CH_RELAY] > config.RELAY_ON_RAW
                was_us, was_lv = sv1.cur_us, [s.level for s in svs]   # journal 写前快照
                us = 1950 if lv else 1050
                sv1.set_us(us)              # 高位1950us吸合 / 低位1050us断开
                state.relay_on, state.relay_us = lv, us   # /state 镜像: 目标值(退化硬件读不到回读)
                for s in svs:
                    s.set_level(lv)
                _relay_journal(sv1, svs, was_us, was_lv)
                # 音量已改由 App 软件控制(右拨杆物理漂移会乱改, 09-09 删除)
                # D 键已退役: 喊话开始/停止由 App 软件控制; 10-17 继电器从 D 键移到 B 键
                a_high = ch[config.CH_ARM] > config.ARM_ON
                if link_gen != state.link_gen:    # (重)连上狗: 回到上锁态, 防重连瞬间狗自行动作
                    link_gen = state.link_gen
                    stand_req = False             # 撤销挂起的姿态请求, 防重解锁后自动执行
                    lie_req = False
                    if armed:
                        armed = False
                        state.rc_armed = False
                        d.hold()                  # 停走保持姿态, 不趴下(09-29 用户要求)
                        journal.record("event", ev="disarm", src="relink")
                        log("链路重连, 已回上锁态(保持站立), 请重新解锁")
                    prev_high = a_high
                if prev_high is None:             # 首帧只记录电平, 不判定解锁边沿
                    prev_high = a_high
                if armed and not a_high:
                    armed = False
                    state.rc_armed = False
                    stand_req = False             # 上锁即撤销挂起的姿态请求(解锁不做自动动作)
                    lie_req = False
                    d.set_mode(0, now=now)
                    journal.record("event", ev="disarm", src="rc")
                    _ev("rc", "disarm")
                    j_must = True
                    log("已上锁(RC)")
                elif not armed and a_high and not prev_high:
                    # A 去抖(10-20): 解锁上升沿距上次解锁边沿太近则忽略(边沿仍刷新锚点,
                    # 连续抖动期内一直忽略, 稳定超过 ARM_DEBOUNCE_S 的翻拨才解锁)
                    if (last_arm_edge is not None
                            and now - last_arm_edge < config.ARM_DEBOUNCE_S):
                        journal.record("event", ev="arm_debounced",
                                       dt=round(now - last_arm_edge, 3))
                        _ev("rc", "arm_debounced", "%.2fs" % (now - last_arm_edge))
                    else:
                        armed = True              # 只认翻拨动作, A 一直停在解锁位不算
                        state.rc_armed = True
                        state.web_armed = False   # RC 接管, 网页控制让位
                        journal.record("event", ev="arm", src="rc")
                        _ev("rc", "arm")
                        j_must = True
                        log("已解锁(RC)")
                    last_arm_edge = now
                prev_high = a_high
                stand = ch[config.CH_STAND] > config.STAND_ON   # C 电平每帧都算(上锁期也跟)
                if armed:
                    # 姿态请求标志+每帧重试(10-20 供电口事故): C 边沿只置标志, set_mode 被
                    # 互锁/起立保护期拦下(返回 False)时标志保留, 到点自动重试, 不会因边沿
                    # 只来一次就丢请求; C 回对面电平则撤销。趴下只认 C 上->下边沿(旧的
                    # "armed 且 C 在下每帧自动趴"路径已删, 解锁不再自动做任何动作);
                    # C 稳在上的补同步保留(开机首连 mode=1, 解锁即可控)
                    if stand and not stand_prev:
                        stand_req = True          # C 下->上边沿: 起立请求
                    if not stand and stand_prev:
                        lie_req = True            # C 上->下边沿: 趴下请求
                    handled = False
                    if stand_req:
                        handled = True
                        if not stand:
                            stand_req = False     # C 已回下位: 撤销起立请求
                        elif d.set_mode(2, now=now):
                            stand_req = False
                    if lie_req:
                        handled = True
                        if stand:
                            lie_req = False       # C 已回上位: 撤销趴下请求
                        elif d.set_mode(0, now=now):
                            lie_req = False
                    if not handled and stand and d.mode != 0:
                        d.set_mode(2, now=now)    # C 稳在上且狗本是站/走: 补同步模式(开机首连 mode=1)
                    g = pos(ch[config.CH_GEAR], 3) + 1   # 三档拨档 -> 挡1/2/3
                    if g != last_gear:
                        last_gear = g
                        state.gear = g
                    # 步态只在站立时同步: 趴着切挡(含趴着解锁, 无论 C 在上在下)只记账,
                    # 否则 TrotRun 等步态指令会把趴下的狗直接弹起来(10-10 实测 bug)
                    if stand and d.mode != 0 and gear_sent != g:
                        gear_sent = g
                        d.set_gear(g)
                    vx_max, vy_max, vyaw_max = config.GEAR_SPEEDS.get(
                        g, (config.VX_MAX, config.VY_MAX, config.VYAW_MAX))
                    ax = axis(ch[config.CH_PITCH]) * config.SIGN_PITCH
                    ay = axis(ch[config.CH_ROLL]) * config.SIGN_ROLL
                    az = axis(ch[config.CH_YAW]) * config.SIGN_YAW
                    # B/D 键动作: 拨到高位的边沿触发一次, 摇杆回中时才响应
                    for cid, (name, dur) in config.KEY_ACTIONS.items():
                        high = ch[cid] > 1002
                        if high and not key_high.get(cid) and not (ax or ay or az):
                            d.trick(name, dur)
                        key_high[cid] = high
                    d.move(ax * vx_max, ay * vy_max, az * vyaw_max)
                stand_prev = stand   # 每帧更新(含上锁期): 解锁瞬间才不会把陈旧电平当边沿
        if f is None or bad:                # 无帧/坏帧都照常补发到点的延迟指令
            d.tick()
        if _SHADOW is not None:             # 影子 tick: 干净帧同 now 幂等 drain, 兼网页摇杆;
            _SHADOW.tick(now if f is not None and not bad else time.monotonic())

        gap_clean = now - last_clean
        state.sbus_ok = (now - last_any) < 1.0     # 链路活着(坏帧也算)
        state.sbus_clean = gap_clean < 1.0          # 数据干净
        # journal 帧采样(轻): 每帧一条, 每 N 帧落盘 1 条; 坏帧/A键边沿帧必落盘
        if f is not None:
            jf_n += 1
            if j_must or jf_n % _J_EVERY_N == 0:
                journal.record("frame",
                               source="netrc" if nf is not None else "sbus",
                               ch=ch, fs=fs, lost=lost,
                               gap_clean=round(gap_clean, 3))
        if gap_clean > config.FS_STOP_S:  # 脏/丢帧: 输出安全关断(脉宽1050+IO低), 防断联冻结在开
            was_us, was_lv = sv1.cur_us, [s.level for s in svs]       # journal 写前快照
            sv1.set_us(1050)
            state.relay_on, state.relay_us = False, 1050   # /state 镜像: 安全关断目标
            for s in svs:
                s.set_level(False)
            _relay_journal(sv1, svs, was_us, was_lv)
            if armed:
                d.hold()
                if gap_clean > config.FS_DAMP_S:        # 持续 30s 无干净帧: 真失联
                    journal.record("event", ev="disarm", src="fs",
                                   gap=round(gap_clean, 1))
                    log("RC 失联 %.0fs, 上锁停走(保持姿态, 09-29 起不再趴下)" % config.FS_DAMP_S)
                    armed = False
                    state.rc_armed = False
                    stand_req = False         # 上锁即撤销挂起的姿态请求(与其他上锁路径同语义)
                    lie_req = False
                    d.hold()           # 停走即可, 姿态交给狗自己保持

        if _SHADOW is not None:             # 影子: 同一 gap_clean/now 喂失控保护
            _SHADOW.safety(gap_clean, now)

        # 网页端: 仅 RC 上锁时接管
        if not armed and state.web_armed:
            d.move(state.web_vx * config.VX_MAX,
                   state.web_vy * config.VY_MAX,
                   state.web_vyaw * config.VYAW_MAX)

        if _SHADOW is not None:             # 影子: 帧尾对照, 差异才记日志/journal
            _SHADOW.compare(f, nf, gap_clean)

        time.sleep(max(0.0, 1.0 / config.SEND_HZ - (time.monotonic() - t0)))


def _ver():
    """部署版本号: deploy 上传的 VERSION 文件(git short hash, P1-1 写入); 没有则 None。
    每次请求现读, 部署后无需重启即可看到新版本。"""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"),
                  encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _audio_state():
    """音频状态(P0-2): 拆分后由 go2-audio 服务(P0-1)每秒写 AUDIO_STATE_PATH;
    文件不存在/停更>3s(服务死)/读坏/结构非法 -> ready:false。只读不写。"""
    path = getattr(config, "AUDIO_STATE_PATH", "/run/go2-audio.json")
    try:
        age = time.time() - os.path.getmtime(path)   # 停更=音频服务已死, 不能沿用残留 ready:true
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        if not isinstance(m, dict) or age > 3.0:
            raise ValueError("audio state stale or malformed")
        return {"ready": m.get("ready") is True, "volume": m.get("volume"),
                "talk": m.get("talk") is True, "playing": m.get("playing"),
                "pid": m.get("pid") if isinstance(m.get("pid"), int) else None,
                "age": round(age, 2)}
    except (OSError, ValueError, TypeError):
        return {"ready": False, "volume": None, "talk": False, "playing": None,
                "pid": None, "age": None}


def web_server(d):
    from fastapi import FastAPI, WebSocket
    from fastapi.responses import HTMLResponse, PlainTextResponse
    import uvicorn

    app = FastAPI()

    @app.get("/logs")
    async def logs():
        """控制台日志面板数据: 板端内存环形日志(最近600行), 不用连网线看 journal"""
        return PlainTextResponse("\n".join(_LOG_BUF))

    @app.get("/")
    async def index():
        page = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "static", "index.html")
        with open(page, encoding="utf-8") as fh:
            return HTMLResponse(fh.read())

    @app.get("/sbus")
    async def sbus_debug_page():
        page = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "static", "sbus.html")
        with open(page, encoding="utf-8") as fh:
            return HTMLResponse(fh.read())

    @app.get("/journal")
    async def journal_tail(n: int = 100):
        """黑匣子尾部只读端点: /journal?n=100(上限 500), 直接看最后几行 JSONL。"""
        return PlainTextResponse("\n".join(journal.tail(n)))

    @app.get("/state")
    async def state_json():
        """只读 JSON 状态快照(P0-2): 部署冒烟/诊断的地基, curl 一次拿全量。
        数据来源: state 镜像 + sbus_stats/netrc 快照 + journal 停机标记 + 狗姿态订阅;
        不触发任何硬件写操作。"""
        now = time.monotonic()
        action = state.action       # 与 /ws 推送同口径: 读时刷新动作锁剩余秒
        a_left = 0.0
        if action:
            a_left = state.action_end - now
            if a_left <= 0:
                action, a_left = None, 0.0
        snap = sbus_stats.snapshot(now)      # 顺带裁剪滑窗(与 /ws 同用法)
        net = netrc.snapshot()
        lft = state.last_frame_t
        audio = _audio_state()
        dt = state.dog_state_t
        dog_age = round(now - dt, 2) if dt is not None else None
        return {
            "ver": _ver(), "uptime_s": round(now - _T0, 1),
            "link": state.link, "link_gen": state.link_gen,
            "mode": state.mode, "rc_armed": state.rc_armed,
            "web_armed": state.web_armed, "gear": state.gear,
            "action": action, "action_left": round(a_left, 1),
            "rising": bool(d.rising) if d is not None else False,
            "src": state.last_src,
            "sbus": {"ok": state.sbus_ok, "clean": state.sbus_clean,
                     "fps": snap["fps"], "last_iv": snap["last_iv"],
                     "frame_age": round(now - lft, 3) if lft is not None else None,
                     "total": snap["total"], "fs": snap["fs_cnt"],
                     "lost": snap["lost_cnt"], "gap": snap["gap_cnt"],
                     "ch": snap["ch"]},
            "net": {"ok": net["net_ok"], "rx": net["net_rx"],
                    "lost": net["net_lost"], "seq": net["net_seq"]},
            "relay": {"on": state.relay_on, "us": state.relay_us},
            "audio": audio,
            # 狗真实姿态(rt/sportmodestate): fresh=false 表示订阅没数据/已停更
            "dog": {"mode": state.dog_mode, "height": state.dog_height,
                    "gait": state.dog_gait, "vel": state.dog_vel,
                    "age": dog_age, "n": state.dog_state_n,
                    "fresh": dog_age is not None and dog_age < config.DOG_STATE_STALE_S},
            "pids": {"bridge": os.getpid(), "audio": audio["pid"],
                     "audio_age": audio["age"]},
            "last_stop": journal.last_stop(),
            "last_cmd": state.last_cmd,
            "last_events": list(_EVENTS),
            "sys": _sys_snapshot(),
        }

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        log("网页端接入")
        last_status = 0.0
        try:
            while True:
                try:
                    m = await asyncio.wait_for(sock.receive_json(), timeout=1.0)
                except asyncio.TimeoutError:
                    m = None
                if m is not None:
                    if "cmd" in m:
                        c = m["cmd"]
                        if state.rc_armed:
                            pass                      # RC 优先, 网页指令忽略
                        elif c == "arm":
                            state.web_armed = True
                            journal.record("event", ev="arm", src="web")
                            if state.mode == 0:
                                d.set_mode(1)
                        elif c in ("disarm", "damp"):
                            state.web_armed = False
                            journal.record("event", ev="disarm", src="web")
                            d.set_mode(0)
                        elif c == "stand":
                            d.set_mode(1)
                        elif c == "walk":
                            d.set_mode(2)
                        if _SHADOW is not None:
                            _SHADOW.web_cmd(c)        # 影子: 同步网页指令(输出不比)
                    elif "vx" in m:   # 摇杆消息三键齐全才赋值; 心跳{ping:1}不得清零速度
                        state.web_vx = clamp01(m.get("vx", 0.0))
                        state.web_vy = clamp01(m.get("vy", 0.0))
                        state.web_vyaw = clamp01(m.get("vyaw", 0.0))
                        if _SHADOW is not None:
                            _SHADOW.web_cmd("axes", vx=m.get("vx", 0.0),
                                            vy=m.get("vy", 0.0), vyaw=m.get("vyaw", 0.0))
                now = time.monotonic()
                if now - last_status > 0.5:
                    last_status = now
                    action = state.action       # 推送前刷新动作锁剩余秒
                    a_left = 0.0
                    if action:
                        a_left = state.action_end - now
                        if a_left <= 0:
                            action, a_left = None, 0.0
                    await sock.send_json({
                        "link": state.link, "mode": state.mode,
                        "rc": state.rc_armed, "sbus": state.sbus_ok,
                        "web": state.web_armed,
                        "sbus_clean": state.sbus_clean, "gear": state.gear,
                        "action": action, "action_left": round(a_left, 1),
                        "src": state.last_src, "rising": bool(d.rising),
                        "dog": {"mode": state.dog_mode, "height": state.dog_height,
                                "gait": state.dog_gait,
                                "age": (round(now - state.dog_state_t, 2)
                                        if state.dog_state_t is not None else None),
                                "fresh": (state.dog_state_t is not None
                                          and now - state.dog_state_t
                                          < config.DOG_STATE_STALE_S)},
                        **sbus_stats.snapshot(), **netrc.snapshot()})
        except Exception:
            pass
        finally:
            log("网页端断开, 解除网页控制并趴下")
            if state.web_armed:
                journal.record("event", ev="disarm", src="web")
            state.web_armed = False
            state.web_vx = state.web_vy = state.web_vyaw = 0.0
            if not state.rc_armed:
                d.set_mode(0)
            if _SHADOW is not None:
                _SHADOW.web_cmd("closed")

    uvicorn.run(app, host="0.0.0.0", port=config.WEB_PORT, log_level="warning")


def trick_udp_loop(gate):
    """本地 TRICK 请求口(P0-1/T2.2): go2-audio 服务(pa_recv 独立进程)把 App 动作请求
    转成一行 "TRICK 名 时长" 发到 127.0.0.1:TRICK_PORT, 本线程调动作网关并把
    OK/LINKDOWN/NOTSTAND 回给它 —— 与拆分前的进程内 trick_cb 注入完全同口径。
    只绑回环: 外部网络碰不到; 名字过 config.TRICKS 白名单(与 pa_recv 的 App 侧检查对齐)。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", config.TRICK_PORT))
    except Exception as e:
        log("TRICK 本地口 %d 起不来, App 动作将不可用: %r" % (config.TRICK_PORT, e))
        return
    log("TRICK 本地口就绪: UDP 127.0.0.1:%d (go2-audio -> 桥)" % config.TRICK_PORT)
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
                log("TRICK 网关异常:", parts[1], repr(e))
        try:
            sock.sendto(r.encode("utf-8"), addr)
        except Exception:
            pass


def main():
    global dog, _SHADOW

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
        log("警告: journal 黑匣子目录不可写, 本次运行不落盘")
    else:
        _ev("sys", "boot", "last_stop=%s" % journal.last_stop())
        log("黑匣子上次停机标记:", journal.last_stop())
    link = DogLink()
    dog = Dog(link, now=_T0)   # 传进程启动锚点: 启用大动作互锁+起立保护期(10-20)
    if config.FSM_SHADOW:
        try:
            _SHADOW = FsmShadow(dog)   # 先于 web/pa 线程: 启动期指令从第一条起就有影子
        except ImportError:
            log("core/fsm.py 不可用, FSM 影子模式停用(部署清单需补 core/, 旧路径不受影响)")
    def _trick_gate(name, dur):
        # 给 App 分别人话回包: 链路断(狗收不到) vs 条件不满足(未解锁/非行走/占用中)
        if state.link != "up":
            return "LINKDOWN"
        ok = dog.trick(name, dur)
        if _SHADOW is not None:
            _SHADOW.trick(name, dur)   # 影子: 同步动作状态(输出不比)
        return "OK" if ok else "NOTSTAND"

    # P0-1/T2.2: 音频拆到独立服务 go2-audio(跑 pa_recv.py), 本进程不再 pa_recv.start(),
    # 也不注入回调; App 动作改走本地 UDP(TRICK_PORT), App "RC" 轮询由音频服务 HTTP 读 /state。
    # rc_loop 的 pa 形参仅留签名兼容(测试在用), 函数体不碰音频。
    threading.Thread(target=trick_udp_loop, args=(_trick_gate,), daemon=True).start()
    netrc.start(getattr(config, "NETRC_PORT", 15010), log)
    threading.Thread(target=web_server, args=(dog,), daemon=True).start()
    log("go2-bridge 启动: 网页端口=%d, SBUS=%s, 连狗方式=%s"
        % (config.WEB_PORT, config.SBUS_PORT, config.METHOD))
    rc_loop(dog, None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
