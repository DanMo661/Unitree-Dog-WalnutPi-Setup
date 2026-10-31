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
import concurrent.futures
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
from servo import Servo, GpioOut, RelayPwm
from pa import Pa          # P0-1/T2.2 后 main 不再用 Pa, 但测试经 bridge.Pa 构造, 保留导出
import netrc
from languard import NetGuard

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

# 候选遥测探测(10-31): 第二客户端实测部分话题只发第一个连接, 挂桥自己连接上记
# 首包确认这台固件发什么; 只诊断不进 state, 固件将来开始发也能自动浮出
_PROBE_SEEN = set()
PROBE_TOPICS = ("BMS_STATE", "SELF_TEST", "SERVICE_STATE", "LIDAR_STATE",
                "SECONDARY_IMU", "WIRELESS_CONTROLLER", "UWB_STATE")

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


def clamp01(v):
    v = float(v)
    return max(-1.0, min(1.0, v))


_LAST_ROBOT_IP = None
_LAST_SCAN_T = 0.0


def _scan_9991(prefix, timeout=0.3):
    """扫描 prefix.0/24 找开着 9991 的狗(只在与板子同网段的当前 WiFi/热点上扫)。"""
    def probe(i):
        ip = "%s.%d" % (prefix, i)
        try:
            s = socket.create_connection((ip, config.SIGNAL_PORT), timeout)
            s.close()
            return ip
        except OSError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        for r in ex.map(probe, range(1, 255)):
            if r:
                return r
    return None


def _pick_robot_ip(timeout=0.8):
    """解析 ROBOT_IP: 支持 IPv4 直填或主机名(如 Unitree.local, mDNS)。
    依次尝试: 上次成功 IP -> 同网段 mDNS 地址 -> 扫描当前 /24 的 9991 ->
    其它网段 mDNS 地址(有线兜底)。这样家里 WiFi/手机热点切换、拔网线都不用改配置，
    且优先走当前 WiFi/热点(与板子默认路由同网段)。"""
    global _LAST_ROBOT_IP, _LAST_SCAN_T
    host = config.ROBOT_IP
    try:
        socket.inet_aton(host)
        return host
    except OSError:
        pass

    def try_ip(ip):
        global _LAST_ROBOT_IP
        try:
            s = socket.create_connection((ip, config.SIGNAL_PORT), timeout)
            s.close()
            if ip != _LAST_ROBOT_IP:
                _LAST_ROBOT_IP = ip
                log("狗地址解析: %s -> %s" % (host, ip))
            return ip
        except OSError:
            return None

    if _LAST_ROBOT_IP:
        if try_ip(_LAST_ROBOT_IP):
            return _LAST_ROBOT_IP
        log("狗地址不可达, 清空上次解析:", _LAST_ROBOT_IP, lvl="debug")
        _LAST_ROBOT_IP = None
    try:
        infos = socket.getaddrinfo(host, config.SIGNAL_PORT,
                                   socket.AF_INET, socket.SOCK_STREAM)
    except OSError as e:
        log("狗地址解析失败:", host, repr(e), lvl="warn")
        infos = []
    ips = []
    for _fam, _typ, _proto, _canon, sa in infos:
        if sa[0] not in ips:
            ips.append(sa[0])
    local = None
    try:                               # 默认路由源地址(家里 WiFi/热点都在 wlan0)
        us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        us.connect(("8.8.8.8", 80))
        local = us.getsockname()[0]
        us.close()
    except OSError:
        pass
    prefix = local.rsplit(".", 1)[0] if local else None
    same = [ip for ip in ips if prefix and ip.startswith(prefix + ".")]
    other = [ip for ip in ips if ip not in same]
    for ip in same:                    # 当前 WiFi/热点上的地址优先
        if try_ip(ip):
            return ip
    now = time.monotonic()
    if prefix and now - _LAST_SCAN_T >= 15.0:   # 限流: 狗不在线时别每 3s 扫全网
        _LAST_SCAN_T = now
        log("扫描 %s.0/24 找狗..." % prefix)
        found = _scan_9991(prefix)
        if found:
            log("扫描找到狗:", found)
            _LAST_ROBOT_IP = found
            return found
    for ip in other:                   # 有线等其它网段兜底
        if try_ip(ip):
            return ip
    return None


def _path_ok(tcp=True):
    """板->狗物理链路探测: 当前 WiFi/热点载波; tcp=True 时再探狗信令口(阻塞式)"""
    try:
        with open("/sys/class/net/%s/carrier" % config.ETH_IFACE) as f:
            if f.read().strip() != "1":
                return False
    except OSError:
        pass                      # 接口不存在/读不到就只靠 TCP 探测
    if not tcp:
        return True
    return _pick_robot_ip() is not None


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
        self._roll_n = 0        # 启动以来完成统计的秒数(趋势图启动零桶垫平用)

    def _roll(self, sec):
        """秒桶翻转: 结算旧秒并补零推进(链路停了也照推, 柱状图不说谎)。须持锁调用"""
        if self._sec0 is None:
            self._sec0 = sec
            return
        if sec == self._sec0:
            return
        self.hist[self._hist_i] = self._sec_n
        self._roll_n += 1
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
            hist = hist[:-1]   # 丢"进行中的当前秒": 只计了部分帧, 画进趋势会形成右端规律性低点
            done = min(self._roll_n, len(hist))
            if done < len(hist):   # 启动未满一窗: 从未统计过的零桶按当前水平延展(首个实测秒
                pad = hist[-1] or int(self.fps)   # 是爬坡值, 拿它垫会前半段贴地)
                for i in range(len(hist) - done):
                    hist[i] = pad
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
        self._nm_q = None             # 非 Move 串行 worker 队列(同一时刻只一条在途)
        self.conn = None
        self._last_logged = None      # LOG_CMDS 去重: 连续相同的指令只记一条
        self._last_gear_read = None   # 心跳回读的挡位(变化才记日志)
        self._mv_n = 0                # journal 采样计数: Move 指令每 N 条记 1 条
        self.drop_link = 0            # submit 时链路未 up/事件循环已收摊 -> 丢弃计数
        self.drop_ttl = 0             # 队列 TTL/互锁/撤销丢弃计数(/state ctl 暴露)
        self._drop_log_t = 0.0        # 丢弃日志限流锚点(5s 一条, 不刷屏/不刷 fsync)
        self._urgent_tasks = set()    # 收权指令(StopMove/StandDown)的即时发送 task
        self._cancel_t = 0.0          # 最近一次撤销起立类指令的时刻(monotonic)
        threading.Thread(target=self._run, daemon=True).start()

    def stats(self):
        """队列水位与丢弃计数(P1 /state ctl 用): 任何线程读都只取长度, 不阻塞。"""
        def n(q):
            try:
                return q.qsize()
            except Exception:
                return 0
        return {"q_depth": n(self.q) + n(self._nm_q),
                "drop_link": self.drop_link, "drop_ttl": self.drop_ttl}

    def _run(self):
        if not DOG_LIB:
            state.link = "lib_missing"
            log("unitree_webrtc_connect 未安装, 连狗功能停用 (bash install.sh)", lvl="error")
            return
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.q = asyncio.Queue()
        self._nm_q = asyncio.Queue()
        self.loop.run_until_complete(self._session())

    async def _session(self):
        kw = {}
        # 事件循环活性打点先起: 覆盖连接尝试/会话/重连全程。rc_loop(独立线程)据此
        # 检测循环停摆 —— 10-21 实测 TF 卡 D 状态把循环卡死 8 分钟, 心跳/看门狗
        # 全在循环里跟着死, 横幅却还显示可控, 必须有循环外的人喊一嗓子
        asyncio.ensure_future(self._loop_ticker())
        if config.AES_KEY:
            kw["aes_128_key"] = config.AES_KEY
        fails = 0            # 连续连接失败次数(退避用)
        while True:
            state.link = "connecting"
            try:
                # 物理链路恢复前不发起连接, 防半开连接把 connect 挂死
                n = 0
                while not await self._probe():
                    state.link = "down"
                    if n % 10 == 0:
                        log("到狗链路不通(网线/狗未开机), 等待恢复...", lvl="debug")
                    n += 1
                    await asyncio.sleep(2)
                ip = _pick_robot_ip()
                if not ip:
                    state.link = "down"
                    await asyncio.sleep(2)
                    continue
                kw["ip"] = ip             # 每次重连重新解析: WiFi/热点切换后自动换地址
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
                fails = 0
                log("WebRTC 已连接", lvl="state")
                self._subscribe_dog_state()   # 先订阅真实姿态, 再决定 reset 的本地 mode
                await self._wait_dog_state()
                if dog is not None:
                    dog.reset()          # (重)连上后狗侧实际状态未知, 用新鲜高度对账本地 mode
                while not self.q.empty():
                    self.q.get_nowait()  # 丢弃重连瞬间残留的旧指令
                while self._nm_q is not None and not self._nm_q.empty():
                    self._nm_q.get_nowait()
                await self._guarded_consume()
            except Exception as e:
                state.link = "down"
                log("WebRTC 会话结束, %ss 后重连:" % (3 * min(fails + 1, 5)), repr(e), lvl="warn")
                fails += 1
            await self._close_conn()
            # 退避(2026-10-29): 狗死了但网口载波还在时, 原先固定 3s 无限重试;
            # 3/6/9/12/15s 递增, 连上即清零 —— 狗真回来时最多多等 12s, 平时不再空转
            await asyncio.sleep(3 * min(fails, 5) if fails else 3)

    async def _guarded_consume(self):
        """_consume/看门狗/心跳 三方赛跑: 拔网线靠看门狗, 会话半死靠心跳, 谁先出事都强拆重连"""
        worker = asyncio.ensure_future(self._consume())
        nmworker = asyncio.ensure_future(self._nonmove_worker())
        watch = asyncio.ensure_future(self._watch_link())
        beat = asyncio.ensure_future(self._heartbeat())
        done, _ = await asyncio.wait({worker, nmworker, watch, beat},
                                     return_when=asyncio.FIRST_COMPLETED)
        urgent = list(getattr(self, "_urgent_tasks", ()))   # 收权即时 task: 拆会话一并取消
        for t in (worker, nmworker, watch, beat):
            t.cancel()
        for t in urgent:
            t.cancel()
        await asyncio.gather(worker, nmworker, watch, beat, *urgent,
                             return_exceptions=True)
        if watch in done:
            raise RuntimeError("看门狗: 到狗链路断开")
        if beat in done:
            raise RuntimeError("心跳: 狗应用层无响应, 会话半死")
        if nmworker in done:
            raise RuntimeError("非 Move worker 退出")
        raise RuntimeError("接收循环退出: %r" % worker.exception())

    async def _watch_link(self):
        """每秒看载波; 每3秒才探一次狗信令口(免得一天打狗8.6万次连接)。
        WiFi 短暂掉载波(漫游/驱动抖动)先等 CARRIER_GRACE_S, 期间恢复则保持会话;
        持续丢失才拆。TCP 探测连续 2 次失败才拆, 狗忙时单次失败不误杀。"""
        n = 0
        tcp_miss = 0
        down_since = None
        while True:
            await asyncio.sleep(1.0)
            n += 1
            now = time.monotonic()
            if not await self._probe(False):
                if down_since is None:
                    down_since = now
                    log("看门狗: %s 载波丢失, %.0fs 内恢复则保持会话"
                        % (config.ETH_IFACE, config.CARRIER_GRACE_S), lvl="debug")
                elif now - down_since >= config.CARRIER_GRACE_S:
                    log("看门狗: %s 载波持续丢失 %.0fs, 拆会话"
                        % (config.ETH_IFACE, now - down_since), lvl="warn")
                    return
                continue
            if down_since is not None:
                log("看门狗: %s 载波恢复(丢失 %.1fs), 保持会话"
                    % (config.ETH_IFACE, now - down_since), lvl="state")
                down_since = None
                tcp_miss = 0
            if n % 3 == 0:
                if await self._probe(True):
                    if tcp_miss:
                        log("看门狗: 狗信令口恢复", lvl="state")
                    tcp_miss = 0
                else:
                    tcp_miss += 1
                    log("看门狗: 狗信令口探测失败 %d/2" % tcp_miss, lvl="debug")
                    if tcp_miss >= 2:
                        log("看门狗: 狗信令口连续 2 次不可达, 拆会话", lvl="warn")
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
                    log("心跳回读挡位:", data, lvl="debug")
            except asyncio.TimeoutError:
                miss += 1
                log("心跳无回包 %d/%d" % (miss, config.HEARTBEAT_MISS), lvl="debug")
                if miss >= config.HEARTBEAT_MISS:
                    log("狗应用层连续无响应, 判会话半死, 强制重连", lvl="error")
                    return
            except Exception as e:
                miss += 1     # 发送本身异常(重连窗口期常见)同样计一次失联
                log("心跳发送异常 %d/%d:" % (miss, config.HEARTBEAT_MISS), repr(e)[:80], lvl="warn")
                if miss >= config.HEARTBEAT_MISS:
                    return

    async def _loop_ticker(self):
        """事件循环活性打点(0.5s 一拍): 循环卡死时打点冻结, rc_loop 侧的
        _loop_alive_check 据此报警。空转成本一次属性赋值。"""
        while True:
            state.loop_ts = time.monotonic()
            await asyncio.sleep(0.5)

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
                log("已订阅狗姿态:", topic, lvl="debug")
            for key, cb in (("LOW_STATE", self._on_low_state),
                            ("MAIN_BOARD_STATE", self._on_mb_state)):
                topic = RTC_TOPIC.get(key)
                if not topic:
                    continue
                self.conn.datachannel.pub_sub.subscribe(topic, cb)
                log("已订阅狗信息:", topic, lvl="debug")
            # 候选遥测探测(10-31): 第二客户端实测部分话题只发第一个连接(bmsstate 等),
            # 挂在桥自己(第一客户端)上记首包, 确认这台固件到底发什么; 不进 state
            for key in PROBE_TOPICS:
                topic = RTC_TOPIC.get(key)
                if not topic:
                    continue
                self.conn.datachannel.pub_sub.subscribe(topic, self._probe_cb(topic))
                log("已订阅探测:", topic, lvl="debug")
        except Exception as e:
            log("订阅狗姿态失败(起立判定回落超时兜底):", repr(e), lvl="warn")

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
            imu = d.get("imu_state")
            if isinstance(imu, dict):
                r = imu.get("rpy")
                if isinstance(r, (list, tuple)) and len(r) >= 3:
                    state.dog_rpy = [float(x) for x in r[:3]]
                if isinstance(imu.get("temperature"), (int, float)):
                    state.dog_imu_t = float(imu["temperature"])
            ec = d.get("error_code")
            if isinstance(ec, (int, float)):
                state.dog_err = int(ec)
            state.dog_state_t = time.monotonic()
            state.dog_state_n += 1
            if state.dog_state_n == 1:
                log("狗姿态首包:", str(d)[:1200], lvl="debug")
        except Exception:
            pass

    def _probe_cb(self, topic):
        """候选话题首包诊断(10-31): 每话题只记一次, 不进 state 不进控制。"""
        def cb(msg, _t=topic):
            if _t in _PROBE_SEEN:
                return
            _PROBE_SEEN.add(_t)
            log("探测首包[%s]:" % _t, str(msg)[:800], lvl="debug")
        return cb

    def _on_low_state(self, msg):
        """lowstate 回调(asyncio 线程): 只取监控用的关节温度/电池摘要, 不碰控制路径。
        首包打 debug 便于核对字段(槽位数/单位与狗固件有关)。"""
        d = msg.get("data") if isinstance(msg, dict) else None
        if not isinstance(d, dict):
            return
        try:
            ms = d.get("motor_state")
            if isinstance(ms, list):
                tj = []
                for mtr in ms:
                    t = mtr.get("temperature") if isinstance(mtr, dict) else None
                    tj.append(float(t) if isinstance(t, (int, float)) else None)
                state.dog_joints = tj
            b = d.get("bms_state")
            if isinstance(b, dict):
                # 实测键名(10-31 全包抓取): soc/cycle/bq_ntc[2]/mcu_ntc[2]/current(-98x
                # 哨兵)/version_*, 无 voltage; 机身温度 temperature_ntc1 在 lowstate 顶层
                clean = {}
                soc = b.get("soc")
                if isinstance(soc, (int, float)) and 0 <= soc <= 100:
                    clean["SOC"] = float(soc)
                cur = b.get("current")
                if isinstance(cur, (int, float)) and cur > -900:
                    clean["current"] = float(cur)
                for k in ("bq_ntc", "mcu_ntc"):
                    v = b.get(k)
                    if (isinstance(v, (list, tuple)) and v
                            and isinstance(v[0], (int, float)) and v[0] > -900):
                        clean[k] = float(v[0])
                cy = b.get("cycle")
                if isinstance(cy, (int, float)):
                    clean["cycle"] = int(cy)
                state.dog_bms = clean or None
            t1 = d.get("temperature_ntc1")
            if isinstance(t1, (int, float)):
                state.dog_mb = float(t1)   # 机身温度(mainboardstate 话题不发, lowstate 自带)
            state.dog_low_t = time.monotonic()
            state.dog_low_n += 1
            if state.dog_low_n == 1:
                log("lowstate 首包:", str(d)[:400], lvl="debug")
        except Exception:
            pass

    def _on_mb_state(self, msg):
        """mainboardstate 回调: 机身温度(temperature_ntc1), 其余字段忽略。
        首包打 debug 核对键名(该话题这台狗可能压根不发)。"""
        d = msg.get("data") if isinstance(msg, dict) else None
        if not isinstance(d, dict):
            return
        try:
            state.dog_mb_n += 1
            if state.dog_mb_n == 1:
                log("mainboardstate 首包:", str(d)[:300], lvl="debug")
            t = d.get("temperature_ntc1")
            if isinstance(t, (int, float)):
                state.dog_mb = float(t)
        except Exception:
            pass

    async def _wait_dog_state(self):
        """(重)连后等一条有效姿态: dog_height 非 None 且在 0.05~0.5m 合理范围,
        再让 dog.reset() 按真实高度对账本地 mode(P0-4: 只有 dog_state_t 不算,
        首包 body_height 可能是 None/离谱值)。超时记日志按未知处理。"""
        t0 = time.monotonic()
        while time.monotonic() - t0 < config.DOG_STATE_WAIT_S:
            if _dog_height_valid():
                return True
            await asyncio.sleep(0.1)
        log("(重)连 %.0fs 内未收到有效狗姿态高度, 本地模式按未知处理(等遥测再对账)"
            % config.DOG_STATE_WAIT_S, lvl="debug")
        return False

    async def _close_conn(self):
        c, self.conn = self.conn, None
        if c is None:
            return
        # v1.x 有 close(), fork 的 v2.0 只有 disconnect() —— 调不存在的 close 会被
        # 静默吞掉, aiortc 的 pc 从不关闭 => 每次重连失败漏一批 ICE UDP socket,
        # 1024 fd 上限几分钟就见底(10-29 第三台狗实测: 967 socket 卡死桥+网页)
        fn = getattr(c, "close", None) or getattr(c, "disconnect", None)
        if fn is None:
            return
        try:
            r = fn()
            if hasattr(r, "__await__"):
                await asyncio.wait_for(r, 8)   # 必须等 pc 真关完才放下一轮
        except Exception as e:
            log("关闭狗连接异常(忽略):", repr(e)[:120], lvl="debug")

    def _drop(self, why, name):
        """队列丢弃计数 + 限流日志(P1: 5s 一条, 不刷控制台/journal/fsync)。"""
        self.drop_ttl += 1
        now = time.monotonic()
        if now - self._drop_log_t >= 5.0:
            self._drop_log_t = now
            log("指令队列丢弃(%s): %s (累计 ttl=%d)" % (why, name, self.drop_ttl), lvl="warn")

    def _cancel_rise_cmds(self):
        """上锁/趴下时撤销 _nm_q 里尚未发出的 StandUp/BalanceStand(收权优先):
        否则 worker 忙完慢请求后迟到的 StandUp 又会把狗拉起来(P0-3 副作用)。
        在 asyncio 线程调用且无 await 点, 与 worker 取件互斥。"""
        self._cancel_t = time.monotonic()
        if self._nm_q is None:
            return
        keep = []
        while not self._nm_q.empty():
            it = self._nm_q.get_nowait()
            if it[0] in ("StandUp", "BalanceStand"):
                self.drop_ttl += 1
                _set_cmd_result(it[0], "dropped")
            else:
                keep.append(it)
        for it in keep:
            self._nm_q.put_nowait(it)

    async def _consume(self):
        """指令分发(P0-3): 保持队列原序, 只合并 Move —— 批次里除最后一条 Move 外
        丢弃, 最后一条 Move 留在它原本的位置(不挪到队尾)。Move 立即发(绝不 wait
        非 Move 的回复, 摇杆不被慢指令堵住); 非 Move 交独立串行 worker。"""
        while True:
            batch = [await self.q.get()]
            while not self.q.empty():
                batch.append(self.q.get_nowait())
            last_mi = -1
            for i, b in enumerate(batch):
                if b[0] == "Move":
                    last_mi = i
            now = time.monotonic()
            for i, (name, p, t_enq) in enumerate(batch):
                if name == "Move" and i != last_mi:
                    continue             # 被同批次后一条 Move 取代
                if name not in ("StopMove", "StandDown"):
                    # 收权指令不受 TTL: 晚到的 StopMove/StandDown 也必须执行
                    ttl = config.MOVE_TTL_S if name == "Move" else config.NONMOVE_TTL_S
                    if now - t_enq > ttl:
                        self._drop("ttl", name)
                        _set_cmd_result(name, "dropped")
                        continue
                if name in ("StopMove", "StandDown"):
                    # 收权路径(P0-3 副作用): 走即时 task, 不排 _nm_q/不受 TTL,
                    # 绝不因 worker 卡在 StandUp 的等回包上而延迟或丢弃
                    if name == "StandDown":
                        self._cancel_rise_cmds()   # 撤销尚未发出的起立类指令
                        state.last_big_sent = now  # 收权也是大动作: 锚点防紧跟着的 StandUp
                    journal.record("cmd", name=name, **p)
                    t = asyncio.ensure_future(self._send(name, p))
                    self._urgent_tasks.add(t)
                    t.add_done_callback(self._urgent_tasks.discard)
                elif name == "Move":
                    await self._send(name, p)      # Move 无 await 点, 直接发
                else:
                    self._nm_q.put_nowait((name, p, t_enq))

    async def _nonmove_worker(self):
        """非 Move 串行 worker: 同一时刻只允许一条在途(保证大动作先后顺序),
        慢请求(最长 REQ_TIMEOUT_S)只堵自己, 不堵摇杆 Move 流。
        发送时刻再校验 MODE_DWELL_S: 用 _last_big_sent(真实发送时刻)而不是入队
        时刻, 防止队列积压后 StandDown -> StandUp 毫秒级背靠背。
        只拦 StandUp: 趴下/补平衡永不延迟(收权优先; BalanceStand 本就紧跟 StandUp)。"""
        while True:
            name, p, t_enq = await self._nm_q.get()
            now = time.monotonic()
            if now - t_enq > config.NONMOVE_TTL_S:
                self._drop("ttl", name)
                _set_cmd_result(name, "dropped")
                continue
            if name in ("StandUp", "BalanceStand") and t_enq < self._cancel_t:
                # 上锁/趴下已发生: 该指令入队在前, 是迟到的起立类指令, 撤销
                self._drop("cancel", name)
                _set_cmd_result(name, "dropped")
                continue
            if name == "StandUp":
                last = state.last_big_sent
                if last is not None and now - last < config.MODE_DWELL_S:
                    self._drop("dwell", name)
                    _set_cmd_result(name, "dropped")
                    continue
            if name in ("StandUp", "StandDown"):
                state.last_big_sent = now   # 大动作真实发送时刻(队列互锁锚点)
            journal.record("cmd", name=name, **p)   # 真正发出才记(cmd 黑匣子)
            await self._send(name, p)

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
                # StopMove code=-1 = 狗无运动可停的常规应答(解锁/趴下转换都有), 降调试级
                log(name, "被狗拒绝 code =", code,
                    lvl="debug" if (name == "StopMove" and code == -1) else "warn")
                _set_cmd_result(name, "rejected:%s" % code)
            else:
                _set_cmd_result(name, "ok")
                if name == "GetSpeedLevel":
                    log("狗侧回读挡位:", r.get("data", {}).get("data"), lvl="debug")
        except asyncio.TimeoutError:
            log("发送超时(狗 %ss 无回包):" % config.REQ_TIMEOUT_S, name, lvl="warn")
            _set_cmd_result(name, "timeout")
        except Exception as e:
            log("发送失败:", name, repr(e), lvl="error")
            _set_cmd_result(name, "error")

    def submit(self, name, **p):
        """入队一条狗指令。返回 True=已入队, False=链路未 up/事件循环已收摊。
        调用方(Dog.set_mode/move/trick)据此决定是否提交本地状态; 失败只计数
        drop_link, 不写 journal(每帧重试也不刷 fsync), 请求标志留给下一帧重试。"""
        if not self.loop or state.link != "up":
            self.drop_link += 1
            return False
        # journal 埋点(kind=cmd): Move 是 20Hz 流, 每 N 条采 1 条(入队时记);
        # 非 Move 在真正发出时记(见 _nonmove_worker) —— 被 TTL/互锁丢弃的不会
        # 伪装成已发出, 每帧重试也不会 20Hz 刷 fsync
        if name == "Move":
            self._mv_n += 1
            if self._mv_n % _J_EVERY_N == 0:
                journal.record("cmd", name=name, **p)
        else:
            state.last_cmd = {"name": name, "ts": time.time(), "result": "pending"}
        # Move 是 20Hz 连发流, 不进 LOG_CMDS(微抖值每条都不同, 会变刷屏+刷TF卡)
        if config.LOG_CMDS and name != "Move" and (name, p) != self._last_logged:
            self._last_logged = (name, p)
            log("指令 ->", name, p if p else "", lvl="debug")
        try:
            self.loop.call_soon_threadsafe(self.q.put_nowait,
                                           (name, p, time.monotonic()))
        except RuntimeError:
            self.drop_link += 1
            return False   # 重连窗口期事件循环已收摊, 丢弃(断链下 submit 本就不该被调)
        return True


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
        self._h_hist = []          # [(包时间戳, body_height)] 最近 0.6s 高度样本
        self._h_ts = None          # 最近采样包的 dog_state_t: 同一遥测包只记一次
        self._no_tele_warn_t = None  # "无姿态遥测"告警限流锚点(monotonic, 30s 一条)
        # 请求重试日志限流(P1): 同一拦截 episode 只记首次拦截 + 最终放行/撤销
        self._blk_reason = None    # 当前拦截原因: stand_guard / interlock / None
        self._blk_target = None    # 被拦的目标模式
        self._blk_until = 0.0      # 拦截解除时刻(monotonic, /state ctl.remain 用)
        self._sync_pending = False # (重)连后对账未决: 等有效高度再定本地 mode(P0-4)
        self._drop_warn_t = None   # "指令未入队"日志限流锚点(P1: 5s 一条, 不 20Hz fsync)

    def _block_episode(self, reason, m, now, until):
        """记一次拦截(调用方持锁); P1 日志限流: 同一 episode 只记首次拦截,
        后续每帧重试只刷新 remain, 不写 journal/不 fsync/不刷控制台。"""
        if self._blk_reason == reason and self._blk_target == m:
            self._blk_until = until
            return
        self._blk_reason, self._blk_target, self._blk_until = reason, m, until
        remain = until - now
        if reason == "stand_guard":
            # 起立保护期(10-20): 开机初期供电口未稳, 拒绝任何会发 StandUp 的请求
            journal.record("event", ev="stand_guard", to=m, remain=round(remain, 3))
            _ev("safety", "stand_guard", "to=%s" % m)
            log("起立保护期: 拒绝起立(开机 %.0fs 内), %.1fs 后到点自动重试"
                % (config.STAND_GUARD_S, remain), lvl="warn")
        else:
            # 大动作互锁(10-20): 两次起立/趴下太近会触发狗供电口保护性断电
            journal.record("event", ev="interlock", to=m, remain=round(remain, 3))
            _ev("safety", "interlock", "to=%s" % m)
            log("互锁: 距上一次起立/趴下 %.2fs < %.1fs, 拒绝(%.1fs 后到点自动重试)"
                % (now - self._last_big, config.MODE_DWELL_S, remain), lvl="warn")

    def _resolve_block(self, m, now):
        """拦截 episode 收尾(调用方持锁): 同一 episode 只记首次拦截 + 最终放行/撤销。
        m == 目标模式记放行; 其他(None/别的模式)记撤销。"""
        if self._blk_reason is None:
            return
        reason, target = self._blk_reason, self._blk_target
        self._blk_reason = self._blk_target = None
        self._blk_until = 0.0
        if m == target:
            journal.record("event", ev="unblock", reason=reason, to=m)
            _ev("safety", "unblock", "%s→%s" % (reason, m))
            log("拦截解除: %s 请求到点放行 -> %s" % (reason, m), lvl="state")
        else:
            journal.record("event", ev="req_cancel", reason=reason, to=m)
            _ev("safety", "req_cancel", "%s" % reason)
            log("拦截撤销: %s 请求已撤销(未执行)" % reason)

    def note_cancel(self, target, now=None):
        """外部撤销一个挂起请求(rc_loop 的 C 回对面电平等): 若正处在针对该目标
        的拦截 episode 里, 补记一条"撤销"并清掉 episode(P1 日志限流)。"""
        now = time.monotonic() if now is None else now
        with self.lock:
            if self._blk_reason is not None and self._blk_target == target:
                self._resolve_block(None, now)

    def set_mode(self, m, now=None):
        """请求切模式。返回 True=已执行或本来就该状态; False=被互锁/起立保护期/
        坐姿规则拦下, 或 submit 入队失败(链路断/事件循环收摊) —— 两种情况都
        未提交本地状态, rc_loop 的每帧重试到点兜住"""
        now = time.monotonic() if now is None else now
        with self.lock:
            if m == self.mode:
                self._resolve_block(m, now)   # 别的模式请求已生效 = 原拦截请求被撤销
                return True
            # 坐姿规则(P1): 坐着时 C 在上位不算数, 拨趴->拨站才起立, 防一坐下就被拉起。
            # 提前返回且不发任何指令: 否则 C 停在上位时 set_mode(2) 每帧一条 StopMove
            if self.mode == 3 and m == 2:
                return False
            # 大动作 = 趴0 <-> 站/走1|2 之间切换(会发 StandUp 或 StandDown);
            # 坐3 本在低位: 3->0 静默 / 3->1 走 BalanceStand, 都不算大动作
            big = (m == 0) != (self.mode == 0)
            # 互锁/保护期只拦"起立"(m!=0): 上锁/趴下任何时候都必须放行 ——
            # 否则 A 键上锁会被静默吞掉, 狗保持最后速度, 收权失效(10-20 复核抓出)
            if big and m != 0 and self._t0 is not None:
                if now - self._t0 < config.STAND_GUARD_S:
                    self._block_episode("stand_guard", m, now,
                                        self._t0 + config.STAND_GUARD_S)
                    return False
                if (self._last_big is not None
                        and now - self._last_big < config.MODE_DWELL_S):
                    self._block_episode("interlock", m, now,
                                        self._last_big + config.MODE_DWELL_S)
                    return False
            prev_mode = self.mode     # journal 用: 切换前旧模式
            # 起立流程中 1->2 只是目标标签对齐: 不清 rising(清了会让 BalanceStand 补发
            # 永久丢失, 且下一帧步态同步会插进物理 StandUp —— 10-21 上装断电事故模式);
            # rise 完成后 rc_loop 的"C 稳在上补同步"自然切 2
            if self.rising and m == 2 and self.mode == 1:
                self._resolve_block(m, now)
                return True
            # 模式一变就作废未到点的延迟指令与起立流程: 起立过程中拨 C 趴下时,
            # 迟到的 BalanceStand 会把趴着的狗瞬间弹起(10-17 实测危险)
            self.pending = []
            self.rising = False
            self._h_hist = []
            self._h_ts = None
            if self.mode == 3 and m == 0:
                # 坐->趴: 狗本来就在低位, 只记状态不发指令(发 StandDown 会从坐姿猛塌)
                self.mode = 0
                self.moving = False
                self.cooldown = 0.0
                state.mode = 0
                state.action = None
                self._sync_pending = False
                self._resolve_block(m, now)
                journal.record("event", ev="mode", prev=prev_mode, to=0)
                log("模式 -> 趴", lvl="state")
                return True
            self.link.submit("StopMove")
            if m == 0:
                ok = self.link.submit("StandDown")
            elif m == 1:
                ok = self._begin_rise(now) if self.mode == 0 \
                    else self.link.submit("BalanceStand")
            elif m == 2:
                ok = self._begin_rise(now) if self.mode == 0 else True
            else:
                ok = True
            if not ok:
                # 入队失败(链路断/事件循环已收摊): 不提交本地状态, 请求标志下一帧重试。
                # 日志/fsync 限流: 20Hz 重试只每 5s 记一条(P1), 计数由 submit drop_link 负责
                if self._drop_warn_t is None or now - self._drop_warn_t >= 5.0:
                    self._drop_warn_t = now
                    journal.record("event", ev="drop_cmd", to=m)
                    _ev("dog", "drop_cmd", "to=%s" % m)
                    log("指令未入队(链路/循环不可用), 模式保持 %s, 等重试" % self.mode, lvl="warn")
                return False
            self.mode = m
            self.moving = False
            self.cooldown = 0.0
            state.mode = m
            state.action = None
            self._sync_pending = False    # 用户已明确指定模式, 不再用遥测覆盖
            self._resolve_block(m, now)
            journal.record("event", ev="mode", prev=prev_mode, to=m)
            _ev("mode", "mode", "%s→%s" % (prev_mode, m))
            log("模式 ->", ("趴", "站", "走", "坐")[m], lvl="state")
            if big and self._t0 is not None:
                self._last_big = now
            return True

    def _begin_rise(self, now):
        """趴->站/走: 发 StandUp, 进入"等高度稳定"状态(调用方持锁)。
        起立期间 move() 被 rising 挡住, 摇杆不会插进起立过程。
        返回 False = StandUp 没入队, 调用方不得进入 rising/不得提交模式。"""
        if not self.link.submit("StandUp"):
            return False
        self.rising = True
        self.rise_t = now
        self.rise_deadline = now + config.STAND_TIMEOUT_S
        self._h_hist = []
        self._h_ts = None
        journal.record("event", ev="stand_up", from_mode=self.mode)
        _ev("dog", "stand_up", "from=%s" % self.mode)
        return True

    def _finish_rise(self, now, why, h):
        """起立完成(调用方持锁): 补 BalanceStand 并解除 rising 锁。
        返回 False = BalanceStand 没入队, 保持 rising 下一 tick 重试。"""
        if not self.link.submit("BalanceStand"):
            return False
        self.rising = False
        self._h_hist = []
        self._h_ts = None
        journal.record("event", ev="balance", why=why,
                       dt=round(now - self.rise_t, 2), height=h)
        _ev("dog", "balance", "%s %.2fs" % (why, now - self.rise_t))
        log("起立稳定 %.2fs ✓ -> 补 BalanceStand" % (now - self.rise_t), lvl="state")
        return True

    def _stand_failed(self, now, h):
        """超时仍没起来(有遥测且高度低于站姿阈值): 绝对不能补 BalanceStand,
        取消起立并把本地模式同步回趴, 等用户重新拨 C(调用方持锁)。"""
        self.rising = False
        self._h_hist = []
        self._h_ts = None
        self.mode = 0
        self.moving = False
        state.mode = 0
        journal.record("event", ev="stand_failed", height=h,
                       dt=round(now - self.rise_t, 2))
        _ev("dog", "stand_failed", "height=%s" % h)
        log("起立失败: 狗未起立(高度 %s < %.2fm), 不发 BalanceStand, 等待重新拨 C"
            % (("%.3fm" % h) if h is not None else "n/a", config.DOG_STAND_HEIGHT), lvl="warn")

    def _maybe_balance(self, now):
        """状态驱动起立完成判定(调用方持锁): 狗自报 body_height 的**不同遥测包**
        在 0.6s 窗口内稳定(至少 3 包且首样本年龄 >=0.5s)且高于站姿阈值 ->
        起立完成。同一包被 20Hz tick 重复读到不算新样本(P0-1: 否则 0.1s 就假稳定)。
        无遥测 -> STAND_TIMEOUT_S 兜底(告警限流); 到点仍低位 -> 起立失败(P0-2)。"""
        if not self.rising:
            return
        if self.mode == 3:
            # P1: 坐姿是持续姿态, 不存在"起立中"; 迟到的 BalanceStand 会把坐姿拉起来
            self.rising = False
            self._h_hist = []
            self._h_ts = None
            return
        h, ts = state.dog_height, state.dog_state_t
        fresh = h is not None and ts is not None and now - ts < config.DOG_STATE_STALE_S
        if not fresh:
            # 完全无遥测: 兜底从 3s 延长到 STAND_TIMEOUT_S(6s), 告警限流 30s 一条
            if now >= self.rise_deadline:
                if self._no_tele_warn_t is None or now - self._no_tele_warn_t >= 30.0:
                    self._no_tele_warn_t = now
                    journal.record("event", ev="no_dog_state",
                                   dt=round(now - self.rise_t, 2))
                    _ev("dog", "no_telemetry", "%.0fs" % config.STAND_TIMEOUT_S)
                    log("告警: 无狗姿态遥测, 起立判定退化为 %.0fs 兜底(本次起立)"
                        % config.STAND_TIMEOUT_S, lvl="warn")
                self._finish_rise(now, "no_telemetry", None)
            return
        if ts != self._h_ts:
            # 只在"新遥测包"到达时记样本; 同一包重复 tick 不增加样本数(P0-1 根因)
            self._h_ts = ts
            self._h_hist.append((ts, h))
        self._h_hist = [(t, v) for t, v in self._h_hist if now - t <= 0.6]
        if (len(self._h_hist) >= 3 and now - self._h_hist[0][0] >= 0.5
                and h > config.DOG_STAND_HEIGHT
                and max(v for _, v in self._h_hist)
                - min(v for _, v in self._h_hist) < config.DOG_HEIGHT_STABLE):
            self._finish_rise(now, "stable", h)
            return
        if now >= self.rise_deadline:
            if h < config.DOG_STAND_HEIGHT:
                # P0-2: 到点还没站起来 -> 绝不补 BalanceStand(会把半起的狗弹起来)
                self._stand_failed(now, h)
            else:
                # 已在高位(站起来了)只是一直不稳: 到点补 BalanceStand 收尾
                self._finish_rise(now, "timeout", h)

    def _apply_reconcile(self, now):
        """(重)连后的本地 mode 对账(调用方持锁, P0-4): 用新鲜高度 + 滞回判定,
        中间区不臆测, 保持 _sync_pending 等下一包。首连无数据才回落到"假定站立"。
        只往 0/1 判, 不直接给 2: 站立后仍要 C 上位补同步才允许摇杆。"""
        if not self._sync_pending or self.rising or now < self.cooldown:
            return
        if not _dog_height_valid(now):
            return
        h = state.dog_height
        if h > config.DOG_STAND_LATCH_H:
            self.mode = 1
        elif h < config.DOG_LIE_LATCH_H:
            self.mode = 0
        else:
            return                    # 滞回中间区: 不猜, 等下一包
        self._sync_pending = False
        state.mode = self.mode
        journal.record("event", ev="dog_state_sync", height=round(h, 3),
                       mode=self.mode)
        _ev("dog", "state_sync", "height=%.3f mode=%s" % (h, self.mode))
        log("遥测对账: 狗真实高度 %.3fm -> 本地 mode=%d(%s)"
            % (h, self.mode, "站立" if self.mode else "趴下"), lvl="debug")

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
        1=FreeWalk自由(慢) 2=FreeWalk自由 3=TrotRun奔跑
        (10-21 用户定档: 挡1 弃经济步态, 1/2 同步态只差限速)"""
        with self.lock:
            gait = {1: "FreeWalk", 2: "FreeWalk", 3: "TrotRun"}.get(g)
            if gait:
                self.link.submit(gait)
                journal.record("event", ev="gear", g=g, gait=gait)
                log("挡位 ->", g, "(%s)" % gait, lvl="state")

    def trick(self, name, dur):
        """花样动作: 仅走模式(站立)可触发; 播放期(冷却)内摇杆/新动作被屏蔽
        返回 True=已触发, False=条件不满足(未解锁/非行走/起立中/动作占用中)"""
        with self.lock:
            now = time.monotonic()
            # rising: 起立流程没完就播动作会插进起立过程; 链路断时 submit 会失败,
            # 不能只收冷却不认账(否则谎报成功)
            if (state.link != "up" or self.mode != 2 or self.rising
                    or self.pending or now < self.cooldown):
                return False
            if not self.link.submit(name):
                return False
            self.cooldown = now + dur
            state.action = name        # 镜像给网页端显示动作锁(只动 state, 不碰统计锁)
            state.action_end = now + dur
            journal.record("event", ev="trick", name=name, dur=dur)
            log("动作 ->", name, "(%ds)" % dur, lvl="state")
            if name == "Sit":
                # 坐下是持续姿态: 记成独立状态, C 拨下(静默)->拨上才会再起立
                self.mode = 3
                state.mode = 3
                journal.record("event", ev="mode", prev=2, to=3)
                log("模式 -> 坐", lvl="state")
            return True

    def move(self, vx, vy, vyaw):
        with self.lock:
            # rising: 起立流程未完成(还等 BalanceStand)时摇杆一律不发, 防插进起立过程
            if self.mode != 2 or self.rising or self.pending or time.monotonic() < self.cooldown:
                return
            if (vx, vy, vyaw) != (0.0, 0.0, 0.0):
                # 只有真入队成功才认账; 失败下一帧重试(链路恢复后立即恢复控制)
                if self.link.submit("Move", x=round(vx, 3), y=round(vy, 3),
                                    z=round(vyaw, 3)):
                    self.moving = True
            elif self.moving and self.link.submit("StopMove"):
                self.moving = False

    def hold(self):
        """失控保护第一步: 幂等停走"""
        with self.lock:
            if self.moving and self.link.submit("StopMove"):
                self.moving = False

    def tick(self):
        """每帧节拍: 重连对账 + 状态驱动起立判定 + 到点的延迟指令。"""
        with self.lock:
            now = time.monotonic()
            self._apply_reconcile(now)
            self._maybe_balance(now)
            due = [t for t in self.pending if now >= t[0]]
            if due:
                self.pending = [t for t in self.pending if now < t[0]]
                for _, name in due:
                    self.link.submit(name)

    def reset(self):
        """WebRTC (重)连上后调用(09-30 用户定案开机行为, P0-4 起每次重连都对账):
        开机首连: 一条指令都不发 —— 狗开机自己站起来, 打断它(哪怕 StopMove)都是干扰;
                  锁定等解锁, 解锁后 C 在上位直接可控(不再补 StandUp)
        断链重连: 只发 StopMove 停走保持姿态(狗可能正按最后杆量盲走, 必须急停)
        两种情形都用新鲜高度对账本地 mode(带滞回): 高 -> 1(站立, 等 C 上位补到 2,
        绝不对已站立的狗发 StandUp); 低 -> 0; 中间/无数据 -> 保持未知等下一包。
        未完成的起立流程一律取消(不补发 BalanceStand), 避免把半起的狗弹起来。"""
        with self.lock:
            now = time.monotonic()
            first = state.link_gen <= 1
            if not first:
                self.link.submit("StopMove")
            self.moving = False
            self.cooldown = 0.0
            self.pending = []
            self.rising = False
            self._h_hist = []
            self._h_ts = None
            state.action = None
            self._resolve_block(None, now)   # 重连复位: 未决拦截 episode 记一次撤销
            self._sync_pending = True        # 先挂对账, 有效高度一到就定 mode
            self._apply_reconcile(now)
            if self._sync_pending and first and not _dog_height_valid(now):
                # 首连没有任何有效高度: 只有这一种情况回落到旧行为"假定站立";
                # 对账标志保留, 迟到的遥测仍会纠正(中间区不猜)
                self.mode = 1
                state.mode = 1
                log("首连无有效姿态数据, 按旧逻辑假定站立(等遥测再对账)", lvl="debug")
            state.mode = self.mode
            journal.record("event", ev="reset", first=first, mode=self.mode)
            log("开机首连: 已锁定等待解锁(不发任何指令)" if first
                else "断链重连: 停走保持姿态, 请重新解锁", lvl="state")


# ---- FSM 影子模式(P0-3 / T1.3): 旧逻辑照跑, core/fsm.py 的 DogFsm 并行对照, 只比不发 ----

_SHADOW = None        # FsmShadow 单例; None = 影子关闭(FSM_SHADOW=False 或 core/ 未部署)
_ACTIVE = None        # FsmActive 单例(T3.4); None = 非 active 模式


def _fsm_mode():
    """执行开关(T3.4): 只认 "active"; 其余(含拼错/旧名 live)一律回落 "shadow"。
    失败方向选旧路径 —— 新状态机没准备好时绝不冒险接管真机。"""
    m = str(getattr(config, "FSM_MODE", "shadow")).strip().lower()
    return "active" if m == "active" else "shadow"


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

    def stats(self):
        return self.inner.stats()     # /state ctl 队列水位透传

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
    submit 接线 = 探针(_probe): FSM 与旧 Dog 同样以"此刻会不会入队成功"决定
    是否提交本地状态(链路断时两边都不提交), 但不真发; 记账与 _RecLink 同口径
    (请求即记, 成败都记) —— 命令序列与状态快照两侧可比。
    事件喂法(接线层契约, 见 core/fsm.py 头注释):
      rc_loop 线程 frame/tick/safety/serial_reopened —— 输出进本帧比较窗口;
      跨线程 link(轮询 state)/web_cmd/trick —— 只同步 FSM 状态, 输出丢弃
      (旧侧对应指令在其他线程发出, 不在 rc_loop 帧窗口内, 无从对比)。"""

    def __init__(self, d):
        from core.fsm import DogFsm   # 惰性导入: 不部署 core/ 时 bridge 仍能启动(影子自停)
        self.lock = threading.Lock()
        self._record = True           # 跨线程事件期间 False: 探针记账同样要"丢弃"
        self.fsm = DogFsm(send=self._probe)
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

    def _probe(self, name, **p):
        """影子探针(只探不发): 回答"此刻 DogLink.submit 会不会入队成功"(与
        submit 的判定同式), FSM 据此与旧 Dog 同步地提交/不提交本地状态。
        记账仅在 rc_loop 帧窗口(_record=True)且与 _RecLink 同口径 —— 请求即记,
        成败都记(旧侧 _RecLink 也是先记再调真 submit)。"""
        ok = state.link == "up" and (
            self._rl is None or getattr(self._rl.inner, "loop", None) is not None)
        if self._record:
            self._out.append(("submit", name, dict(p)))
        return ok

    def wrap_link(self, inner):
        self._rl = _RecLink(inner, threading.current_thread())
        return self._rl

    def wrap_relay(self, sv1, svs):
        self._rr = [_RecRelay(s) for s in [sv1] + list(svs)]
        return self._rr[0], self._rr[1:]

    # ---- 跨线程事件: 只同步状态, 输出丢弃(含探针记账, _record=False) ----

    def link_poll(self):
        with self.lock:
            self._record = False
            try:
                if state.link_gen != self.fsm.link_gen_cur:
                    self.fsm.link(state.link_gen, height=state.dog_height,
                                  height_t=state.dog_state_t)   # 与 dog.reset() 同口径
                if state.link != self._last_link:
                    self._last_link = state.link
                    self.fsm.set_link(state.link == "up")
            finally:
                self._record = True

    def web_cmd(self, cmd, vx=None, vy=None, vyaw=None):
        with self.lock:
            self._record = False
            try:
                self.fsm.web_cmd(cmd, vx=vx, vy=vy, vyaw=vyaw)
            finally:
                self._record = True

    def trick(self, name, dur):
        with self.lock:
            self._record = False
            try:
                self.fsm.set_link(state.link == "up")  # 与 _trick_gate 同刻的链路口径
                self.fsm.trick(name, dur)
            finally:
                self._record = True

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
        now = time.monotonic()
        if getattr(self, "_div_log_t", None) is None or now - self._div_log_t >= 5.0:
            self._div_log_t = now
            ch, fs, lost = f if f is not None else (None, None, None)
            journal.record("fsm_divergence",
                           src=("netrc" if nf is not None else "sbus") if f is not None else None,
                           fs=fs, lost=lost, gap=round(gap_clean, 3),
                           ch=list(ch) if ch else None,
                           old_sub=old_sub, fsm_sub=fsm_sub,
                           old_relay=old_rg, fsm_relay=fsm_rg, fsm=snap)
            log("FSM 差异: 旧=%s 新=%s (fsm=%s)" % (old_sub + old_rg, fsm_sub + fsm_rg, snap),
                lvl="warn")   # 分歧是影子存在的意义, 精简模式也要可见; 5s 限频防逐帧刷屏/journal


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
                log("FSM 影子异常, 已停用影子(旧路径不受影响):", repr(e), lvl="warn")
    return wrap


for _m in ("link_poll", "web_cmd", "trick", "frame", "tick", "safety",
           "serial_reopened", "serial_offline", "compare"):
    setattr(FsmShadow, _m, _shadow_safe(getattr(FsmShadow, _m)))


class FsmActive:
    """FSM 执行开关 active 模式(T3.4): DogFsm 是唯一指令源, 旧 Dog/rc_loop 控制路径停用。

    事件喂法与 FsmShadow 相同(同一把锁)。输出落地分两路:
      submit 类 —— 不在输出列表里: 构造时注入 send=link.submit, FSM 在事件内
                   同步真发(指令日志/journal 由 DogLink.submit 自带), 并按返回值
                   决定是否提交本地状态(入队失败不提交, 与旧 Dog 同门);
      其余输出 —— _exec 落地: ("relay", us) -> Servo.set_us(us);
                   ("gpio", level) -> GpioOut.set_level(level); ("log", msg) -> log(msg)
    互斥保证: FSM_MODE="active" 时 main 只构造本类(不构造 Dog/FsmShadow),
    rc_loop 走 _rc_loop_active —— 同一时刻只有这一条路径能向下发指令。"""

    active = True

    def __init__(self, link, now=None):
        from core.fsm import DogFsm   # 惰性导入: 不部署 core/ 时 main 回落旧路径
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

    # ---- /ctl 与网页端读取的旧路径属性 ----

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
    """按 FSM_MODE 构造唯一控制器(T3.4), 返回 (controller, shadow)。
    active -> (FsmActive, None): 不构造 Dog, 也不构造影子(互斥, 无双向下发);
    shadow -> (Dog, FsmShadow|None): 现行为。core/fsm.py 缺失时一律回落旧路径。"""
    if _fsm_mode() == "active":
        try:
            return FsmActive(link, now=now), None
        except ImportError:
            log("core/fsm.py 不可用, FSM active 无法接管, 回落旧路径(不会空转)", lvl="warn")
    d = Dog(link, now=now)
    sh = None
    if config.FSM_SHADOW:
        try:
            sh = FsmShadow(d)
        except ImportError:
            log("core/fsm.py 不可用, FSM 影子模式停用(部署清单需补 core/, 旧路径不受影响)", lvl="warn")
    return d, sh


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
    """FSM_MODE=active 主循环(T3.4): 串口/netrc/节拍/journal 与旧 rc_loop 同构,
    控制决策与指令全部来自 DogFsm; 旧 Dog 的 set_mode/move/trick 在这里一次都不调用 —
    与旧路径执行互斥(同一进程内不会有第二处 submit/set_us/set_level)。"""
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


def rc_loop(d, pa):
    global _SHADOW
    if getattr(d, "active", False):
        return _rc_loop_active(d)    # T3.4: active = FSM 唯一指令源, 旧循环整体不执行
    if config.FSM_SHADOW and _SHADOW is None:
        try:
            _SHADOW = FsmShadow(d)   # main() 已建则复用; 测试直跑 rc_loop 时在此创建
        except ImportError:
            log("core/fsm.py 不可用, FSM 影子模式停用(部署清单需补 core/, 旧路径不受影响)", lvl="warn")
    sbus = Sbus(config.SBUS_PORT, on_frames=sbus_stats.feed)
    # 解锁状态唯一状态源: /state 与 App 查询都读它(GIL 下单属性赋值即原子)
    state.rc_armed = False
    last_clean = time.monotonic()   # 最近干净帧时刻: 失控保护计时锚点(hold/趴下都看它)
    last_any = time.monotonic()     # 最近任意帧(含坏帧)时刻: 只反映链路是否活着
    link_gen = 0          # WebRTC 连接代数: 每次重连都强制重新解锁
    prev_high = None      # A 键上一帧电平(None=首帧只记录), 解锁只认上锁->解锁的翻拨边沿
    last_src = None       # 帧来源(sbus/netrc): 换源首帧只记电平, 跨源电平差不合成边沿
    last_gear = 0         # 拨档当前位(0=未同步; 趴下切挡只记账, 起立时补同步)
    gear_sent = 0         # 已真正发给狗的挡位(趴下时拨档变化不发送, 防步态指令把狗弹起来)
    stand_prev = True     # C 键上一帧电平(True=首帧前视为稳态, 防把开机电平当边沿)
    stand_req = False     # C 上沿起立请求: 被 set_mode 拦下(互锁/保护期)时保留, 每帧重试
    lie_req = False       # C 下沿趴下请求: 同上(趴下无保护期)
    last_arm_edge = None  # A 解锁上升沿时刻(去抖锚点; 上锁下降沿永不去抖)
    arm_dbg_logged = False  # A 去抖拦截 episode 是否已记首次(P1: 不每帧刷 journal)
    key_high = {}         # B 键电平记忆: 拨到高位的边沿才触发动作
    jf_n = 0              # journal 帧计数(每 N 帧采 1 帧; 坏帧/A键边沿帧不采样, 必记)
    svs = [GpioOut(g, log) for g in config.RELAY_GPIOS]   # 38/40/37/35脚: IO电平
    sv1 = RelayPwm([Servo(config.SERVO_CHIP, ch,          # B键继电器: PWM1+PWM2 双路同值并出
                          config.SERVO_MIN_US, config.SERVO_MAX_US, log)
                    for ch in (config.SERVO1_CH, config.SERVO2_CH)])
    if _SHADOW is not None:              # 影子: 旧路径硬件/指令出口套只读透传记录器(语义不变)
        sv1, svs = _SHADOW.wrap_relay(sv1, svs)
        d.link = _SHADOW.wrap_link(d.link)
    while True:
        _loop_alive_check(time.monotonic())
        if not sbus.opened:
            if sbus.open():
                log("SBUS 串口已打开:", config.SBUS_PORT)
                state.rc_armed = False               # 串口重开必须重新解锁, 防插拔误触发
                state.gear = None
                prev_high = None
                stand_req = False           # 全新会话: 撤销挂起的姿态请求, 防解锁后自动执行
                lie_req = False
                d.note_cancel(2, time.monotonic())   # 拦截 episode 收尾(撤销)
                arm_dbg_logged = False
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
            src = "netrc" if nf is not None else "sbus"
            if src != last_src and not (fs or lost):   # 坏帧不播种(脏值会合成假 C/B 边沿), 下一好帧再播
                # 换源首帧只记电平不当边沿: App 与实体遥控同时在线时, 跨源电平差不得合成
                # A/C/B 键边沿(防 UDP 丢包窗口 1Hz 解锁-趴下振荡、App 切后台回来时拿
                # SBUS 电平合成解锁边沿自动起立); 真实翻拨发生在单一源连续帧里不受影响
                last_src = src
                prev_high = None
                stand_prev = ch[config.CH_STAND] > config.STAND_ON
                key_high = {cid: ch[cid] > config.KEY_THRESHOLD
                            for cid in config.KEY_ACTIONS}
            if fs or lost:                  # 坏帧: 链路活着但数据脏, 不作控制输入
                bad = True
                j_must = True
                last_any = now
                if state.rc_armed:
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
                    d.note_cancel(2, now)         # 拦截 episode 收尾(撤销)
                    arm_dbg_logged = False
                    if state.rc_armed:
                        state.rc_armed = False
                        d.hold()                  # 停走保持姿态, 不趴下(09-29 用户要求)
                        journal.record("event", ev="disarm", src="relink")
                        log("链路重连, 已回上锁态(保持站立), 请重新解锁", lvl="state")
                    prev_high = a_high
                if prev_high is None:             # 首帧只记录电平, 不判定解锁边沿
                    prev_high = a_high
                if state.rc_armed and not a_high:
                    state.rc_armed = False
                    stand_req = False             # 上锁即撤销挂起的姿态请求(解锁不做自动动作)
                    lie_req = False
                    d.set_mode(0, now=now)
                    d.note_cancel(2, now)         # 拦截 episode 收尾(撤销)
                    journal.record("event", ev="disarm", src="rc")
                    _ev("rc", "disarm")
                    j_must = True
                    log("已上锁(RC)", lvl="state")
                elif not state.rc_armed and a_high and not prev_high:
                    # A 去抖(10-20): 解锁上升沿距上次解锁边沿太近则忽略(边沿仍刷新锚点,
                    # 连续抖动期内一直忽略, 稳定超过 ARM_DEBOUNCE_S 的翻拨才解锁)。
                    # P1 日志限流: 同一抖动 episode 只记首次, 不 20Hz 写 journal/fsync
                    if (last_arm_edge is not None
                            and now - last_arm_edge < config.ARM_DEBOUNCE_S):
                        if not arm_dbg_logged:
                            arm_dbg_logged = True
                            journal.record("event", ev="arm_debounced",
                                           dt=round(now - last_arm_edge, 3))
                            _ev("rc", "arm_debounced", "%.2fs" % (now - last_arm_edge))
                            log("A 键抖动: 解锁边沿距上次 %.2fs < %.1fs, 已拦(稳定后重翻)"
                                % (now - last_arm_edge, config.ARM_DEBOUNCE_S), lvl="warn")
                    else:
                        state.rc_armed = True              # 只认翻拨动作, A 一直停在解锁位不算
                        state.web_armed = False   # RC 接管, 网页控制让位
                        arm_dbg_logged = False
                        journal.record("event", ev="arm", src="rc")
                        _ev("rc", "arm")
                        j_must = True
                        log("已解锁(RC)", lvl="state")
                        # 解锁即可控(10-31 用户): 解锁瞬间 C 已在上位则视同 C 上沿起立请求,
                        # 省掉"解锁后再拨一下 C"(开机狗自站立场景尤其繁琐); C 在下位则
                        # 保持趴/坐不动, 想起立照旧拨 C。坐姿(3)除外: 尊重坐姿规则
                        # ("C 上位不算数", 防坐下即被拉起), 照旧等 C 下->上边沿。
                        # 复用 stand_req 每帧重试: 互锁/起立保护期到点自动放行。
                        if d.mode != 3 and ch[config.CH_STAND] > config.STAND_ON:
                            stand_req = True
                    last_arm_edge = now
                    state.arm_blocked_until = now + config.ARM_DEBOUNCE_S
                prev_high = a_high
                stand = ch[config.CH_STAND] > config.STAND_ON   # C 电平每帧都算(上锁期也跟)
                if state.rc_armed:
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
                            d.note_cancel(2, now)  # 拦截 episode 收尾(记撤销)
                        elif d.set_mode(2, now=now):
                            stand_req = False
                    if lie_req:
                        handled = True
                        if stand:
                            lie_req = False       # C 已回上位: 撤销趴下请求
                        elif d.set_mode(0, now=now):
                            lie_req = False
                    if not handled and stand and d.mode != 0:
                        d.set_mode(2, now=now)    # C 稳在上且狗非趴: 补同步模式(坐姿由 set_mode 坐姿规则拒; 开机首连 mode=1)
                    g = pos(ch[config.CH_GEAR], 3) + 1   # 三档拨档 -> 挡1/2/3
                    if g != last_gear:
                        last_gear = g
                        state.gear = g
                    # 步态只在站立时同步: 趴着切挡(含趴着解锁, 无论 C 在上在下)只记账,
                    # 否则 TrotRun 等步态指令会把趴下的狗直接弹起来(10-10 实测 bug)。
                    # 10-21 补: 起立流程(rising)期间也不发 —— 黑匣子实测 C 上沿后
                    # StandUp 与 EconomicGait 相隔 14ms 并发, 上装随即断电; 等起立完成
                    # (BalanceStand 后 rising=False)的下一帧再同步挡位
                    if stand and d.mode != 0 and not d.rising and gear_sent != g:
                        gear_sent = g
                        d.set_gear(g)
                    vx_max, vy_max, vyaw_max = config.GEAR_SPEEDS.get(
                        g, (config.VX_MAX, config.VY_MAX, config.VYAW_MAX))
                    ax = axis(ch[config.CH_PITCH]) * config.SIGN_PITCH
                    ay = axis(ch[config.CH_ROLL]) * config.SIGN_ROLL
                    az = axis(ch[config.CH_YAW]) * config.SIGN_YAW
                    # B/D 键动作: 拨到高位的边沿触发一次, 摇杆回中时才响应
                    for cid, (name, dur) in config.KEY_ACTIONS.items():
                        high = ch[cid] > config.KEY_THRESHOLD
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
            if state.rc_armed:
                d.hold()
                if gap_clean > config.FS_DAMP_S:        # 持续 30s 无干净帧: 真失联
                    journal.record("event", ev="disarm", src="fs",
                                   gap=round(gap_clean, 1))
                    log("RC 失联 %.0fs, 上锁停走(保持姿态, 09-29 起不再趴下)" % config.FS_DAMP_S, lvl="warn")
                    state.rc_armed = False
                    stand_req = False         # 上锁即撤销挂起的姿态请求(与其他上锁路径同语义)
                    lie_req = False
                    d.note_cancel(2, now)     # 拦截 episode 收尾(撤销)
                    d.hold()           # 停走即可, 姿态交给狗自己保持

        if _SHADOW is not None:             # 影子: 同一 gap_clean/now 喂失控保护
            _SHADOW.safety(gap_clean, now)

        # 网页端: 仅 RC 上锁时接管
        if not state.rc_armed and state.web_armed:
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

    web_guard = NetGuard(token=getattr(config, "WEB_TOKEN", ""), log=log, name="网页遥控")
    web_guard.assert_configured(
        log, "浏览器端谁都能 arm/开走狗; config.py 配 WEB_TOKEN 后访问 /?tok=<串>")

    def _fsm_feed():
        """网页事件喂给唯一的 FSM 句柄: active=FsmActive(执行), shadow=FsmShadow(只同步)"""
        return _ACTIVE if _ACTIVE is not None else _SHADOW

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

    @app.get("/sbus-old.html")
    async def sbus_old_page():
        """旧版调试台回退入口(v1 起随包分发, 只读静态页)"""
        page = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "static", "sbus-old.html")
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
            # 控制输入真身(P1): netrc 新鲜时控制走网络通道, 否则走 SBUS 解码通道
            "ch_source": state.last_src,
            "ch_effective": (net["ch"] if state.last_src == "netrc" and net["ch"]
                             else snap["ch"]),
            "ctl": _ctl_snapshot(now),
            "arm_blocked": round(max(0.0, state.arm_blocked_until - now), 2),
            "relay": {"on": state.relay_on, "us": state.relay_us},
            "audio": audio,
            # 狗真实姿态(rt/sportmodestate): fresh=false 表示订阅没数据/已停更
            "dog": {"mode": state.dog_mode, "height": state.dog_height,
                    "gait": state.dog_gait, "vel": state.dog_vel,
                    "age": dog_age, "n": state.dog_state_n,
                    "fresh": dog_age is not None and dog_age < config.DOG_STATE_STALE_S},
            "dog2": _dog2_snapshot(now),
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
        # 令牌门(2026-10-29 审查): WEB_TOKEN 非空时浏览器必须带 ?tok=<串> 才能遥控。
        # 原先 0.0.0.0:8000 无鉴权, 同网段任何人 {"cmd":"arm"} 就能让狗站起来开走。
        if not web_guard.token_ok(sock.query_params.get("tok")):
            journal.record("event", ev="ws_rejected", detail="token")
            log("网页端令牌不符, 拒绝接入:", sock.client, lvl="warn")
            await sock.close(code=4401)
            return
        log("网页端接入", lvl="state")
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
                            # 统一(10-31): 网页 arm 与 RC 解锁同语义 —— 直接到可控,
                            # 不再"先 arm 到站立平衡、再点行走才吃摇杆"(趴下时走起立流程)
                            d.set_mode(2)
                        elif c in ("disarm", "damp"):
                            state.web_armed = False
                            journal.record("event", ev="disarm", src="web")
                            d.set_mode(0)             # 趴下永远允许
                        elif c in ("stand", "walk"):
                            # P1: 起立流程未完成时明确拒绝, 不静默清 rising 后放行 Move
                            # (否则摇杆会插进起立过程把狗弹起来); 趴下不受此限
                            if d.rising:
                                await sock.send_json({
                                    "err": "rising", "cmd": c,
                                    "msg": "起立中(%.1fs), 请等 BalanceStand 完成"
                                           % (time.monotonic() - d.rise_t)})
                            else:
                                d.set_mode(1 if c == "stand" else 2)
                        _f = _fsm_feed()
                        if _f is not None:
                            _f.web_cmd(c)             # 影子: 只同步; active: 落地执行
                    elif "vx" in m or "vy" in m or "vyaw" in m:
                        # 摇杆消息只更新带来的键, 缺键不清零(10-31): 网页双摇杆各发
                        # 各的键, 才能边走边转; 心跳{ping:1}三键全缺, 天然不清零
                        if "vx" in m:
                            state.web_vx = clamp01(m["vx"])
                        if "vy" in m:
                            state.web_vy = clamp01(m["vy"])
                        if "vyaw" in m:
                            state.web_vyaw = clamp01(m["vyaw"])
                        _f = _fsm_feed()
                        if _f is not None:
                            _f.web_cmd("axes", vx=m.get("vx"), vy=m.get("vy"),
                                       vyaw=m.get("vyaw"))
                now = time.monotonic()
                if now - last_status > 0.5:
                    last_status = now
                    action = state.action       # 推送前刷新动作锁剩余秒
                    a_left = 0.0
                    if action:
                        a_left = state.action_end - now
                        if a_left <= 0:
                            action, a_left = None, 0.0
                    status = {
                        "link": state.link, "mode": state.mode,
                        "rc": state.rc_armed, "sbus": state.sbus_ok,
                        "web": state.web_armed,
                        "sbus_clean": state.sbus_clean, "gear": state.gear,
                        "action": action, "action_left": round(a_left, 1),
                        "src": state.last_src, "rising": bool(d.rising),
                        "ctl": _ctl_snapshot(now),
                        "dog": {"mode": state.dog_mode, "height": state.dog_height,
                                "gait": state.dog_gait,
                                "age": (round(now - state.dog_state_t, 2)
                                        if state.dog_state_t is not None else None),
                                "fresh": (state.dog_state_t is not None
                                          and now - state.dog_state_t
                                          < config.DOG_STATE_STALE_S)}}
                    status.update(sbus_stats.snapshot())
                    # netrc 快照的 ch 是"网络通道真身", 不与 SBUS 通道条(ch)混用:
                    # /state 用 ch_effective 区分, /ws 这里只取链路统计字段
                    status.update({k: v for k, v in netrc.snapshot().items()
                                   if k != "ch"})
                    await sock.send_json(status)
        except Exception:
            pass
        finally:
            # 关网页只停走、保持站姿(2026-10-29 审查: 原为 set_mode(0) 直接趴下 ——
            # 谁打开再关掉页面都会让狗当场卧倒, 站楼梯/斜坡/货架边就是摔机风险;
            # 趴下改为只认显式命令)
            log("网页端断开, 解除网页控制并停走(保持站姿)", lvl="state")
            if state.web_armed:
                journal.record("event", ev="disarm", src="web")
            state.web_armed = False
            state.web_vx = state.web_vy = state.web_vyaw = 0.0
            if not state.rc_armed and state.link == "up":
                d.hold()
            _f = _fsm_feed()
            if _f is not None:
                _f.web_cmd("closed")

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
    global dog, _SHADOW, _ACTIVE

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
    # 执行开关(T3.4): active 只建 FsmActive(旧 Dog/影子都不建); shadow = 现行为。
    # 先于 web/TRICK 线程构造, 启动期指令从第一条起就只有一个来源。
    dog, _sh = _make_controller(link, _T0)
    _SHADOW, _ACTIVE = _sh, (dog if getattr(dog, "active", False) else None)
    log("FSM 执行开关: %s" % ("active(FSM 产生指令, 旧路径停用)" if _ACTIVE is not None
                              else "shadow(旧路径执行%s)" % ("+影子比对" if _SHADOW else "")))
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
    netrc.start(getattr(config, "NETRC_PORT", 15010), log,
                allow=getattr(config, "NETRC_ALLOW", []),
                token=getattr(config, "NETRC_TOKEN", ""))
    threading.Thread(target=web_server, args=(dog,), daemon=True).start()
    log("go2-bridge 启动: 网页端口=%d, SBUS=%s, 连狗方式=%s"
        % (config.WEB_PORT, config.SBUS_PORT, config.METHOD))
    rc_loop(dog, None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
