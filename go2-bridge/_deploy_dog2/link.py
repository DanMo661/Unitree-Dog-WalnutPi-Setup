# -*- coding: utf-8 -*-
"""WebRTC 链路层(狗库导入 + 狗地址解析/物理链路探测 + DogLink 会话线程), 从 bridge.py 平移, 行为零变更"""
import asyncio
import concurrent.futures
import json
import random
import socket
import threading
import time

import config
import journal
from telemetry import state, log, _J_EVERY_N, _set_cmd_result, _dog_height_valid

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
                import bridge   # 惰性导入防循环(bridge 顶层 import link); 会话回调运行时 bridge 已加载完
                if bridge.dog is not None:
                    bridge.dog.reset()   # (重)连上后狗侧实际状态未知, 用新鲜高度对账本地 mode
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
