# -*- coding: utf-8 -*-
"""
Go2 Air (固件>=1.1.7) SBUS 遥控桥
  云卓G12接收机 SBUS -> 本板解码 -> WebRTC(App同款通道) -> Go2
可选浏览器遥控页: http://<开发板IP>:8000   (RC解锁时 RC 优先)

用法: python3 main.py
依赖: pip install unitree_webrtc_connect pyserial fastapi uvicorn
"""
import asyncio
import json
import math
import threading
import time

try:
    from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
    from unitree_webrtc_connect.constants import RTC_TOPIC
except ImportError as e:
    raise SystemExit(
        "缺少依赖或库的导入路径变了: pip install unitree_webrtc_connect\n"
        "若仍报错, 打开库内示例 examples/go2/data_channel/sportmode_mcf/sportmode_mcf.py, 照抄其 import 行") from e

from sbus import Sbus

# ==================== 需要按你实际情况修改的区域 ====================
METHOD    = "LocalAP"           # "LocalAP": 开发板连狗热点(推荐起步/外场)
                                # "LocalSTA": 狗和板子在同一网络(需填 ROBOT_IP)
ROBOT_IP  = "192.168.123.18"    # LocalSTA 时狗的地址(网线直连狗前部网口即此值)
SBUS_PORT = "/dev/ttyUSB0"      # SBUS串口, 用 ls /dev/ttyUSB* 查看

# 通道分配: 先跑 sbus_test.py 看清每个通道的原始值, 再回来改
CH_ROLL, CH_PITCH, CH_YAW = 0, 1, 2   # 左摇杆横=侧移, 左摇杆纵=前后, 右摇杆横=转向
CH_MODE, CH_ARM = 4, 7                # 三段开关(趴/站/走), 两段解锁开关
CH_AVOID = 5                          # 两段开关(避障), 参数确认前不生效
ARM_ON   = 1600                       # CH_ARM 高于此值=解锁
ENABLE_AVOID = False                  # 跑通库内交互示例确认参数名后再改 True

VX_MAX, VY_MAX, VYAW_MAX = 0.5, 0.3, 1.0   # 初期限速! 熟练后逐步放开
SEND_HZ = 20
WEB, WEB_PORT = True, 8000

# 固件>=1.1.7 的 MCF 指令号 (出处: 仓库示例 sportmode_mcf.py)
API = {"Damp": 1001, "BalanceStand": 1002, "StopMove": 1003, "StandUp": 1004,
       "StandDown": 1005, "Euler": 1007, "Move": 1008, "SpeedLevel": 1015,
       "SwitchAvoidMode": 2058}

# 避障/速度档的参数名: 先跑一遍库内交互示例 sportmode_mcf.py 确认后修正
def p_avoid(on): return {"on": on}
def p_level(lv): return {"level": lv}
# ===================================================================


def axis(raw, lo=172, hi=1811, dz=0.06):
    """通道原始值(172~1811) -> -1..1, 带死区"""
    v = max(-1.0, min(1.0, (raw - 992) / ((hi - lo) / 2)))
    if abs(v) < dz:
        return 0.0
    return (v - math.copysign(dz, v)) / (1 - dz)


def pos(raw, n=3):
    """n段开关原始值 -> 0..n-1"""
    return min(n - 1, max(0, round((raw - 172) / (1811 - 172) * (n - 1))))


class DogBridge:
    """WebRTC会话跑在后台线程, 断线自动重连; submit() 供同步代码投递指令"""
    def __init__(self):
        self.q = None
        self.loop = asyncio.new_event_loop()
        self.ok = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        if not self.ok.wait(60):
            raise RuntimeError("WebRTC连接超时: 检查开发板是否连上狗的网络/热点")

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.q = asyncio.Queue()
        self.loop.run_until_complete(self._session())

    async def _session(self):
        while True:
            try:
                kw = {"ip": ROBOT_IP} if METHOD == "LocalSTA" else {}
                self.conn = UnitreeWebRTCConnection(
                    getattr(WebRTCConnectionMethod, METHOD), **kw)
                await self.conn.connect()
                await self.conn.datachannel.wait_for_connection()
                self.ok.set()
                print("WebRTC 已连接")
                while True:
                    item = await self.q.get()
                    while item[0] == "Move" and not self.q.empty():
                        item = self.q.get_nowait()      # 连续Move只发最新一条
                    await self._send(item)
            except Exception as e:
                self.ok.clear()
                print("WebRTC 断开, 5s后重连:", e)
                await asyncio.sleep(5)

    async def _send(self, item):
        name, p = item
        try:
            await self.conn.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["SPORT_MOD"],
                {"api_id": API[name], "parameter": json.dumps(p)})
        except Exception as e:
            print("发送失败:", name, e)

    def submit(self, name, **p):
        if self.ok.is_set():
            asyncio.run_coroutine_threadsafe(self.q.put((name, p)), self.loop)


class Dog:
    """模式状态机: 0趴 1站 2走; 模式指令只在状态变化时发送一次"""
    def __init__(self, bridge):
        self.b = bridge
        self.mode = 0            # 上电按"趴下"对待: 解锁时先StandUp, 稳妥
        self.pending = []        # [(触发时间, 指令名, 参数dict)]
        self.moving = False

    def submit(self, name, **p):
        self.b.submit(name, **p)

    def set_mode(self, m):
        if m == self.mode:
            return
        self.submit("StopMove")
        if m == 0:
            self.submit("Damp")
        elif m == 1:
            self.submit("StandUp" if self.mode == 0 else "BalanceStand")
        elif m == 2:
            if self.mode == 0:   # 从趴下进行走: 先起立, 等3s再进平衡站立
                self.submit("StandUp")
                self.pending.append((time.time() + 3.0, "BalanceStand", {}))
            else:
                self.submit("BalanceStand")
        self.mode = m
        self.moving = False
        print("模式 ->", ["趴", "站", "走"][m])

    def move(self, vx, vy, vyaw):
        if self.mode != 2 or self.pending:
            return
        if (vx, vy, vyaw) != (0.0, 0.0, 0.0):
            self.submit("Move", x=vx, y=vy, z=vyaw)
            self.moving = True
        elif self.moving:
            self.submit("StopMove")
            self.moving = False

    def tick(self):
        now = time.time()
        keep = []
        for t, name, kw in self.pending:
            if now >= t:
                self.submit(name, **kw)
            else:
                keep.append((t, name, kw))
        self.pending = keep


web = {"armed": False, "vx": 0.0, "vy": 0.0, "vyaw": 0.0}

PAGE = """<!doctype html><meta charset=utf-8><title>Go2 WebRC</title>
<style>body{background:#111;color:#eee;font-family:sans-serif;text-align:center}
.z{display:inline-block;width:260px;height:260px;background:#222;border-radius:50%;margin:14px}
button{font-size:18px;margin:8px;padding:12px 26px}#e{background:#c00}</style>
<div class="z" id="l"></div><div class="z" id="r"></div><br>
<button onclick="c('arm',true)">解锁</button><button onclick="c('arm',false)">上锁</button>
<button onclick="c('stand')">站立</button><button onclick="c('walk')">行走</button>
<button id="e" onclick="c('damp')">趴下/急停</button>
<script src="https://cdn.jsdelivr.net/npm/nipplejs@0.10.2/dist/nipplejs.min.js"></script>
<script>
const ws=new WebSocket(`ws://${location.host}/ws`), s=f=>f>1?1:f;
const mk=(id,fn)=>{const j=nipplejs.create({zone:document.getElementById(id),
  mode:'static',position:{left:'50%',top:'50%'},size:220});
  j.on('move',(e,d)=>{const r=d.angle.radian;fn(s(d.force),r)});
  j.on('end',()=>fn(0,0))};
mk('l',(f,r)=>ws.send(JSON.stringify({vx:Math.sin(r)*f,vy:Math.cos(r)*f,vyaw:0})));
mk('r',(f,r)=>ws.send(JSON.stringify({vx:0,vy:0,vyaw:Math.cos(r)*f})));
const c=(k,v)=>ws.send(JSON.stringify({cmd:k,on:v}));
</script>"""


def web_server(dog):
    from fastapi import FastAPI, WebSocket
    from fastapi.responses import HTMLResponse
    import uvicorn

    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        while True:
            try:
                m = await sock.receive_json()
            except Exception:
                web.update(armed=False, vx=0.0, vy=0.0, vyaw=0.0)
                break
            if "cmd" in m:
                c = m["cmd"]
                if c == "arm":
                    web["armed"] = bool(m.get("on"))
                    if web["armed"] and dog.mode == 0:
                        dog.set_mode(1)
                    elif not web["armed"]:
                        dog.set_mode(0)
                elif c == "stand":
                    dog.set_mode(1)
                elif c == "walk":
                    dog.set_mode(2)
                elif c == "damp":
                    web["armed"] = False
                    dog.set_mode(0)
            else:
                web.update(vx=m.get("vx", 0.0), vy=m.get("vy", 0.0),
                           vyaw=m.get("vyaw", 0.0))

    uvicorn.run(app, host="0.0.0.0", port=WEB_PORT, log_level="warning")


def rc_loop(dog):
    sbus, last_frame, armed, avoid_state = Sbus(SBUS_PORT), 0.0, False, None
    while True:
        latest = None
        while (p := sbus.read()) is not None:
            latest = p
        now = time.time()

        # ---- RC失控保护: 仅在RC解锁状态下生效; 无有效帧0.2s停走, 2s趴下 ----
        if armed and (latest is None or latest[1] or latest[2]):
            if now - last_frame > 0.2 and dog.moving:
                dog.submit("StopMove")
                dog.moving = False
            if now - last_frame > 2.0 and dog.mode != 0:
                dog.set_mode(0)
            time.sleep(0.01)
            latest = None          # 无效帧不作为控制输入

        if latest is not None:
            ch, _, _ = latest
            last_frame = now
            dog.tick()

            # ---- 解锁总开关 ----
            if armed and not (ch[CH_ARM] > ARM_ON):
                armed = False
                web["armed"] = False
                dog.set_mode(0)
                print("已上锁")
            if ch[CH_ARM] > ARM_ON and not armed:
                armed = True
                print("已解锁")
                dog.set_mode(pos(ch[CH_MODE]))

            if armed:
                # ---- 模式开关(三段) ----
                dog.set_mode(pos(ch[CH_MODE]))

                # ---- 避障开关(边沿触发, 参数确认后开启 ENABLE_AVOID) ----
                if ENABLE_AVOID:
                    av = ch[CH_AVOID] > 1500
                    if av != avoid_state:
                        avoid_state = av
                        dog.submit("SwitchAvoidMode", **p_avoid(av))

                # ---- 速度指令(仅行走模式) ----
                dog.move(axis(ch[CH_PITCH]) * VX_MAX,
                         axis(ch[CH_ROLL]) * VY_MAX,
                         axis(ch[CH_YAW]) * VYAW_MAX)
        else:
            dog.tick()
            time.sleep(0.01)

        # ---- 网页端: 仅当RC未解锁时生效 ----
        if not armed and web["armed"]:
            dog.move(web["vx"] * VX_MAX, web["vy"] * VY_MAX, web["vyaw"] * VYAW_MAX)

        time.sleep(1.0 / SEND_HZ)


def main():
    dog = Dog(DogBridge())
    if WEB:
        threading.Thread(target=web_server, args=(dog,), daemon=True).start()
    rc_loop(dog)


if __name__ == "__main__":
    main()
