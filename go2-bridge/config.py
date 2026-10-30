# -*- coding: utf-8 -*-
"""所有可调参数集中在这 —— 改完 sudo systemctl restart go2-bridge 生效"""
import sys

# ---- 连接狗的方式 ----
# "LocalSTA": 狗和板子连同一路由器/WiFi。家里调试用这个, 板子留在 192.168.8.21 可 SSH。
# "LocalAP":  板子连狗热点 GO2-XXXXXX(外场推荐), 此时板子离开家网, SSH/网页远程断开。
METHOD   = "LocalSTA"
ROBOT_IP = "192.168.123.161"  # LocalSTA 用: 实测本狗 webrtc_bridge 在 .161:9991 (con_notify 新协议; .18 是旧文档示例, 本狗不通)
AES_KEY  = ""                 # 仅固件 >=1.1.15 需要(32位hex, 库内 fetch_aes_key.py 获取); 1.1.1~1.1.14 留空
ETH_IFACE   = "eth0"          # 连狗网口(看门狗监视网线载波, 松动即刻发现)
SIGNAL_PORT = 9991            # 狗 webrtc_bridge 信令口(con_notify), 看门狗 TCP 探测用
NETRC_PORT = 15010            # 网络遥控: App 杆量通道(UDP, 新鲜时优先于 SBUS 线)

# ---- 控制面鉴权(2026-10-29 代码审查: 原为 0.0.0.0 全开放, 同网段任何设备可解锁开走狗) ----
# 两项都留空 = 保持旧行为(完全开放, 启动时打一条告警); 配了就强制校验。
# 配法示例(按现场填):
#   NETRC_ALLOW = ["192.168.8.0/24"]        # 只收这个网段的杆量包(手机 App 所在网段)
#   NETRC_TOKEN = "换成你自己的随机串"       # App 若支持带 tok 字段则一并校验
#   WEB_TOKEN   = "换成你自己的随机串"       # 浏览器访问 http://<板>:8000/?tok=<串>
#   PA_ALLOW    = ["192.168.8.0/24"]        # 喊话/上传只收这个网段
NETRC_ALLOW = []
NETRC_TOKEN = ""
WEB_TOKEN   = ""
PA_ALLOW    = []

# ---- 狗会话心跳闭环(09-29: 狗侧会话半死时信令活着但运动指令进黑洞, 横幅仍显示可控) ----
HEARTBEAT_S       = 5.0   # 心跳周期 s: 发有回包的 GetSpeedLevel 探狗应用层
HEARTBEAT_TIMEOUT = 4.0   # 单次心跳等回包超时 s
HEARTBEAT_MISS    = 2     # 连续 miss 此次数 → 判会话半死 → 自动拆会话重连(09-29 两次实测: 重连即愈)
REQ_TIMEOUT_S     = 10.0  # 普通指令等回包上限 s(防狗不回包时把指令管线冻死)
CARRIER_GRACE_S = 5.0       # 载波短暂丢失容忍秒(WiFi 抖动/漫游); 持续丢失超过此值才拆会话

# ---- SBUS 串口 ----
SBUS_PORT = "/dev/ttyS2"      # 核桃派1B 40pin 串口(丝印RX2=pin10) 直入, 09-19 实测 100Hz SBUS 流

# ---- 通道映射(2026-09-05 实测标定: 中值1002, 行程282~1722; 显示值=1050~1950, 1500<->1002) ----
# 右摇杆左右=ch0(右+), 右摇杆上下=ch2(上+), 左摇杆左右=ch3(右+), 左摇杆上下=ch1(下+)
# 按键(A->ch4 B->ch5 C->ch6 D->ch8, 自锁两档): A=1002/1722, B/C/D=282/1722
CH_ROLL, CH_PITCH, CH_YAW = 3, 1, 0   # 侧移=左横 / 前后=左竖 / 转向=右横 (左手移动, 右手转向)
SIGN_ROLL, SIGN_PITCH, SIGN_YAW = -1, -1, -1   # 方向符号(09-05实狗验证定稿): 前后左竖上推=前进, 左横右推=右移, 右横右推=右转
CH_ARM                    = 4         # A 键: 解锁/上锁 (1002=上锁, 1722=解锁)
ARM_ON = 1350                         # A 两档是 1002/1722, 阈值必须夹在中间
CH_STAND                  = 6         # C 键: 站立/趴下 (282=趴, 1722=站, 仅解锁后有效)
STAND_ON = 1000                       # C 高于此=站
CH_GEAR                   = 9         # 三档拨档: 282=挡1 1002=挡2 1722=挡3 (步态+限速联动)
# ---- SBUS 原始值标定(2026-09-05 实测; 换遥控器/接收机须重跑 sbus_monitor.py 重标, 只改这里) ----
AXIS_MID      = 1002    # 摇杆中位原始值: axis() 的零点(显示值 1500 <-> 1002)
AXIS_SPAN     = 720     # 摇杆半行程: 中位到满杆的原始值差(满杆 1722/282 = 1002±720)
POS_MIN       = 172     # 两档/拨档低位原始值: pos() 映射行程下端(实测 172)
POS_MAX       = 1811    # 两档/拨档高位原始值: pos() 映射行程上端(实测 1811)
KEY_THRESHOLD = 1002    # B/D 键"高位"判定阈值: 两档 282/1722 的中间值
# ---- B/D 键(自锁两档 282/1722) ----
CH_B                      = 5         # B=飞控继电器输出(10-17 让位, 握手动作改 App 触发)
KEY_ACTIONS = {}                      # B/D 键动作全退役: D=旧继电器(已移B键), B=现继电器
# ---- App 动作面板: TRICK 协议白名单(名字=SPORT_CMD_MCF 键, 值=占用秒数) ----
# 09-16 用户拍板: 危险动作(翻跟头/倒立/前扑/前跳)全部移除, 只留安全项
# TRICK_AUDIO: 触发动作时同步播放的板上预设音频(名字须与板上 pa-files 一致, 空串=不放)
TRICKS = {
    # 时长=动作占用锁(冷却), 宁长勿短: 短了锁提前放行会把两个舞叠到狗上(09-19 实测翻车)
    # 先按配乐长度兜底, 两支舞的真实舞步时长待下次遛狗掐表校准
    "Dance1":     24,   # 摇摆舞(配乐: 基米斗, 24s)
    "Dance2":     39,   # 舞蹈(配乐: 基米斗地主, 39s)
    "Hello":      4,    # 握手
    "Sit":        3,    # 坐下(持续姿态, C 拨上起立)
    "Stretch":    5,    # 伸懒腰
    "Content":    4,    # 撒娇
    "Scrape":     5,    # 刮挠
    "Heart":      5,    # 比心
}
TRICK_AUDIO = {
    "Dance1": "基米斗",
    "Dance2": "基米斗地主",
}
# ---- 狗载荷输出 (10-17 定稿: 飞控继电器=RC PWM解码型, 50Hz舵机脉冲流, 1650us 分界) ----
# B键(ch5) 两档 -> PWM1+PWM2 双路同值并出: B高=两路都1950us(>1650 继电器吸合) / B低=两路都1050us(断开)
# 2026-10-24 统一两台狗: dog2 航模继电器接 pwm2(16脚) / dog3 接 pwm1(15脚), 并出后一份代码通吃
# 15脚(PI11)=PWM1, 16脚(PI12)=PWM2; 38/40/37/35脚普通IO电平并联跟随B键; 8脚(PI5)=SBUS串口占用不可用
SERVO1_CH                 = 1         # PWM1 = 15脚(PI11), 与 PWM2 一起由 B 键驱动
SERVO2_CH                 = 2         # PWM2 = 16脚(PI12), 同上(双路并出, 键位差异退役)
CH_RELAY                   = 5        # B键 -> pwm1+pwm2 同值脉宽 + 38/40/37/35脚GPIO
RELAY_GPIOS                = [269, 270, 260, 259]   # 38/40/37/35脚 GPIO电平
RELAY_ON_RAW               = 1350     # GPIO 判定阈值(脉宽走 1050/1950 两端)
# ---- 舵机(PI13=PWM3): 仅左拨杆, 全程等比例 0~180°(中位=90°); 不依赖解锁 ----
SERVO_CHIP, SERVO_CH      = 0, 3
SERVO_MIN_US, SERVO_MAX_US = 500, 2500   # 脉宽 0.5~2.5ms = 0~180°(个别舵机行程不同可微调)
# ---- 喊话音量: 全部由遥控器 App 滑条控制(板端不再从拨杆读) ----
# 2026-09-09 喇叭 15W -> 3W: 上限 30%, 超了会破音甚至烧喇叭(App 端同步封顶)
DEFAULT_VOLUME = 25      # 默认音量 %(软件增益线性映射; 功放旋钮请另固定 50% 以下)
PA_VOL_MAX   = 100       # 音量硬上限 %, 板端强制钳制(09-30 用户解除 3W 喇叭 30% 限制)
# ---- 预设音频库容量(板子 32G 卡实测 26G 空闲, 60s 录音约 3.8MB/条) ----
PA_MAX_FILES = 200       # 板上最多条数, 超出拒绝上传(ERR MAXFILES)
PA_MIN_FREE_MB = 1000    # 磁盘剩余低于此值(MB)拒绝上传(ERR NOSPACE)
# ---- 喊话音频流(遥控器App -> 板子): 音频 UDP 裸 PCM + 控制 UDP 行协议 ----
PA_UDP_PORT  = 15000     # 音频接收端口(板子在 144 段的 IP 上收)
PA_CTRL_PORT = 15001     # 控制端口: HELLO 探活 / VOL? 查音量 / VOL n 设音量 / PLAY/LIST/TALK/RATE
PA_UP_PORT   = 15003     # 预设文件上传端口(UPB/UPC/UPE 分块+累计确认)
PA_RATE      = 16000     # 默认采样率 Hz(S16LE 单声道; App 可用 RATE 命令切 8k 省带宽)
# ---- 板级差异: 音频声道策略(dog2 R 声道输出级损坏 / dog3 两声道完好且功放接 R) ----
# True = 左右声道同数据(接线未知或双声道完好都安全); False = L-only(右声道置零, 仅有左声道)
# dog2 板上 config_local.py 写 OVERRIDES = {"PA_STEREO": False}, 本文件保持默认
PA_STEREO    = True
# ---- App 动作转发(P0-1/T2.2: 音频拆独立服务后的本地口) ----
TRICK_PORT   = 15011     # go2-audio(pa_recv) -> bridge 的 TRICK 动作请求口(UDP 127.0.0.1 单播)
# ---- 日志 ----
LOG_CMDS = True       # 记录每条发往狗的指令(连续相同的 Move 自动去重防刷屏)
# ---- 飞行记录仪(journal.py 黑匣子, JSONL 落盘) ----
JOURNAL_ENABLED = True   # 总开关; False 时 main 不初始化, record 未初始化自动空转(零开销)
JOURNAL_DIR     = ""     # 落盘目录; 空=自动选择(板端 /home/pi/go2-bridge/journals, 开发机 ./journals)
JOURNAL_EVERY_N = 5      # 高频流采样比: 每 N 帧记 1 帧 / 每 N 条 Move 记 1 条(坏帧/边沿帧不采样, 必记)
JOURNAL_MAX_MB  = 32     # 单个 journal 文件上限 MB, 超出自动切下一序号文件
# ch11 恒 1722(疑似开关机键), 忽略
# ---- 各挡位限速 (vx m/s, vy m/s, vyaw rad/s): 挡位越高越快 ----
GEAR_SPEEDS = {
    1: (1.0, 0.8, 1.5),     # 自由(慢): FreeWalk 步态(10-21 晚定档: 前后 1.3->1.0, 侧移 1.0->0.8, 转向 1.3->1.5)
    2: (2.0, 1.2, 2.0),     # 自由: FreeWalk(10-21 晚侧移 1.5->1.2)
    3: (3.0, 1.6, 2.6),     # 奔跑: TrotRun(10-21 提速, 原 2.5/1.4/2.5)
}
# B(ch5) D(ch8) 留作备用

# ---- 限速(初期限死, 熟练后逐步放开) ----
VX_MAX, VY_MAX, VYAW_MAX = 0.5, 0.3, 1.0   # m/s, m/s, rad/s

SEND_HZ  = 20       # 主循环频率(Move 指令流上限)
WEB_PORT = 8000     # 浏览器遥控页端口

# ---- /state 诊断端点(P0-2, 只读) ----
AUDIO_STATE_PATH = "/run/go2-audio.json"  # go2-audio 服务(P0-1)每秒写的状态文件; 读不到=audio.ready:false

# ---- 失控保护(仅 RC 解锁期间生效) ----
FS_STOP_S = 0.25    # 无干净帧超过此秒数 -> 立即 StopMove 站定等待恢复
FS_DAMP_S = 30.0    # 无干净帧超过此秒数 -> 真失联: 上锁+停走保持姿态(09-29 用户要求断链不趴下; 仅 A 键主动上锁才趴)并要求重新解锁(09-16: 10s太短, 30s)
                    # (下楼震动只丢帧不短链: 坏帧先站定等恢复, 10s 收不到干净帧才判失联)

# ---- 安全互锁(10-20 供电口事故止血) ----
# 事故背景: 狗对上装板供电, 起立(StandUp)瞬间狗对外供电口保护性断电, 板子掉电重启。
# 已确认两个大动作(StandDown -> StandUp)间隔太近是触发条件之一, 用户手动加延时可避开,
# 现做软件止血。三个常数配套 rc_loop 的"请求标志+每帧重试": 被拦下的请求到点自动执行。
MODE_DWELL_S   = 2.0  # 大动作互锁: 趴下<->起立(会发 StandDown/StandUp 的模式切换)两次
                      # 执行至少间隔此秒数, 不够则拒绝发指令(set_mode 返回 False)
ARM_DEBOUNCE_S = 1.0  # A 键解锁去抖: 解锁上升沿距上一次解锁边沿小于此秒数则忽略
                      # (上锁下降沿永不去抖, 保证任何时候都能上锁趴下收权)
STAND_GUARD_S  = 5.0  # 起立保护期: 进程启动后此秒数内拒绝任何会发 StandUp 的请求
                      # (板子刚上电/重启时供电口尚未稳定, 起立电流冲击易再次触发保护断电)

# ---- 狗真实姿态闭环(10-20: 不再用本地假想状态, 起立完成以狗上报的 body_height 为准) ----
# sportmodestate(rt/sportmodestate) 实时上报 mode/body_height/gait_type/velocity。
# 首连先读真实姿态再决定本地 mode; StandUp 后等 body_height 连续稳定才补 BalanceStand,
# 替代原来写死的 3 秒(第一次起立比后续慢, 固定 3 秒会插进起立过程把狗弹起来)。
DOG_STAND_HEIGHT = 0.22   # body_height 高于此值视为站起(趴约 0.08~0.15, 站约 0.28~0.33)
DOG_HEIGHT_STABLE = 0.004 # 0.6s 窗口内高度波动小于此值 = 已稳定, 可以 BalanceStand
DOG_STATE_STALE_S = 1.0   # sportmodestate 超过此秒数没更新 = 不采信, 回落到超时兜底
STAND_TIMEOUT_S  = 6.0    # 起立后最长等多久补 BalanceStand(无姿态数据/一直不稳时的兜底)
DOG_STATE_WAIT_S = 2.0    # (重)连等待第一条有效 sportmodestate 的上限(超时按未知处理)
# ---- 重连对账(P0-4): 每次 reset 都用新鲜高度对账本地 mode, 带滞回不臆测 ----
DOG_HEIGHT_MIN   = 0.05   # body_height 有效下限(米): 超出范围视为遥测不可信
DOG_HEIGHT_MAX   = 0.50   # body_height 有效上限(米)
DOG_STAND_LATCH_H = 0.28  # 高于此值判"站立" -> 本地 mode=1(等 C 上位补同步到 2, 不发 StandUp)
DOG_LIE_LATCH_H   = 0.16  # 低于此值判"趴下" -> 本地 mode=0; 两阈值之间保持未知, 等下一包再判
# ---- 指令队列(P0-3): Move 高频流与慢指令解耦 ----
MOVE_TTL_S    = 1.0       # Move 入队后超过此秒数未发出即丢弃(摇杆只认最新)
NONMOVE_TTL_S = 3.0       # 非 Move 入队后超过此秒数未发出即丢弃(防迟到大动作)

# ---- FSM 状态机接线(P0-3 / T1.3 影子模式 / T3.4 执行开关) ----
# FSM_MODE 是执行开关, 两条路径互斥, 同一时刻只有一条会向狗下发指令:
#   "shadow" = 现行为: 旧 rc_loop/Dog 执行, DogFsm(core/fsm.py) 影子比对(只比不发);
#   "active" = 新状态机: DogFsm 产生实际指令, 旧 Dog/rc_loop 控制路径停用。
# 真机验证由主代理按用户节奏执行; 本仓库默认始终 shadow。
FSM_MODE   = "shadow"
FSM_SHADOW = True     # 仅 shadow 模式生效: True = 建影子逐帧对照(输出只比不发, 差异记
                      # "FSM 差异" + journal); False = 完全不建影子(零行为零开销)。
                      # active 模式忽略本开关(active 自身就是执行路径, 不会另建影子)

# ---- 板级差异覆盖(config_local.py 不进 git) ----
# 板上按需放 config_local.py: OVERRIDES = {"PA_STEREO": False, ...}
# 只允许覆盖本文件已定义的白名单键(全部大写常量), 未定义键忽略, 防手滑拼错静默生效。
_BASE_KEYS = frozenset(k for k in globals() if k.isupper())
_DENY_KEYS = frozenset({"FSM_MODE"})   # 执行开关不进板级差异: 切 active 必须走
                                       # 主仓 config + 影子零差异 -> 真机验证流程(CLAUDE.md)
try:
    import config_local
except ImportError as _e:
    # 只容忍"没有 config_local.py"(ModuleNotFoundError 且是它自己); 文件在但
    # 自身 import 失败(缺依赖等)必须当场炸出来 —— 静默吞掉会让板上标定悄悄失效
    if not (isinstance(_e, ModuleNotFoundError) and _e.name == "config_local"):
        raise
else:
    for _k, _v in getattr(config_local, "OVERRIDES", {}).items():
        if _k in _DENY_KEYS:
            print("[config] 拒绝 config_local 覆盖 %s: 执行开关不进板级差异(见 CLAUDE.md)"
                  % _k, file=sys.stderr)
        elif _k in _BASE_KEYS:
            globals()[_k] = _v
