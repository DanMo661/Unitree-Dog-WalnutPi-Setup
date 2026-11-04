# -*- coding: utf-8 -*-
"""网页/FastAPI 服务(web_server + 系统健康/版本/音频状态快照), 从 bridge.py 平移, 行为零变更"""
import asyncio
import json
import os
import time

import config
import selfcheck
import journal
import netrc
from languard import NetGuard
from stats import sbus_stats
from telemetry import (log, _LOG_BUF, _EVENTS, _T0, state, clamp01,
                       _dog_height_valid, _dog2_snapshot)


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
        """网页事件喂给唯一的 FSM 句柄(active=FsmActive 执行; 旧路径/影子已删, ADR-0004)"""
        import bridge   # 惰性导入防循环(bridge 顶层 import webserver); _ACTIVE 真身在 bridge
        return bridge._ACTIVE

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

    @app.get("/gas")
    async def gas_json():
        """气体传感器小快照(App 悬浮窗轮询用, 只含气体, 数百字节)。"""
        import bridge
        g = getattr(bridge, "_GAS", None)
        return (g.snapshot(time.monotonic()) if g is not None
                else {"enabled": False, "ok": False, "sensors": []})

    @app.get("/state")
    async def state_json():
        """只读 JSON 状态快照(P0-2): 部署冒烟/诊断的地基, curl 一次拿全量。
        数据来源: state 镜像 + sbus_stats/netrc 快照 + journal 停机标记 + 狗姿态订阅;
        不触发任何硬件写操作。"""
        import bridge   # 惰性导入防循环: _ctl_snapshot 留在 bridge(读 bridge 全局 dog)
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
            "ctl": bridge._ctl_snapshot(now),
            "arm_blocked": round(max(0.0, state.arm_blocked_until - now), 2),
            "relay": {"on": state.relay_on, "us": state.relay_us},
            "gas": (bridge._GAS.snapshot(now)
                    if getattr(bridge, "_GAS", None) is not None
                    else {"enabled": False, "ok": False}),
            "selfcheck": selfcheck.last_snapshot(),
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
                            _f.web_cmd(c)             # 唯一路径: FsmActive 执行并同步镜像(旧 shadow 双轨已删, ADR-0004)
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
                    import bridge   # 惰性导入防循环: _ctl_snapshot 留在 bridge(读 bridge 全局 dog)
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
                        "ctl": bridge._ctl_snapshot(now),
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
