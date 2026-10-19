# -*- coding: utf-8 -*-
"""所有可调参数集中在这 —— 改完 sudo systemctl restart go2-bridge 生效"""

# ---- 连接狗的方式 ----
# "LocalSTA": 狗和板子连同一路由器/WiFi。家里调试用这个, 板子留在 192.168.8.21 可 SSH。
# "LocalAP":  板子连狗热点 GO2-XXXXXX(外场推荐), 此时板子离开家网, SSH/网页远程断开。
METHOD   = "LocalSTA"
ROBOT_IP = "192.168.123.161"  # LocalSTA 用: 实测本狗 webrtc_bridge 在 .161:9991 (con_notify 新协议; .18 是旧文档示例, 本狗不通)
AES_KEY  = ""                 # 仅固件 >=1.1.15 需要(32位hex, 库内 fetch_aes_key.py 获取); 1.1.1~1.1.14 留空
ETH_IFACE   = "eth0"          # 连狗网口(看门狗监视网线载波, 松动即刻发现)
SIGNAL_PORT = 9991            # 狗 webrtc_bridge 信令口(con_notify), 看门狗 TCP 探测用
NETRC_PORT = 15010            # 网络遥控: App 杆量通道(UDP, 新鲜时优先于 SBUS 线)

# ---- 狗会话心跳闭环(09-29: 狗侧会话半死时信令活着但运动指令进黑洞, 横幅仍显示可控) ----
HEARTBEAT_S       = 5.0   # 心跳周期 s: 发有回包的 GetSpeedLevel 探狗应用层
HEARTBEAT_TIMEOUT = 4.0   # 单次心跳等回包超时 s
HEARTBEAT_MISS    = 2     # 连续 miss 此次数 → 判会话半死 → 自动拆会话重连(09-29 两次实测: 重连即愈)
REQ_TIMEOUT_S     = 10.0  # 普通指令等回包上限 s(防狗不回包时把指令管线冻死)

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
# B键(ch5) 两档 -> PWM1 脉宽: B高=1950us(>1650 继电器吸合) / B低=1050us(<1650 断开)
# 15脚(PI11)=PWM1 <- B键; 38/40/37/35脚普通IO电平并联跟随B键; 8脚(PI5)=SBUS串口占用不可用
SERVO1_CH                 = 1         # PWM1 = 15脚(PI11) <- B键
CH_RELAY                   = 5        # B键 -> pwm1脉宽 + 38/40/37/35脚GPIO
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
    1: (0.9, 0.5, 1.5),     # 经济: 原中档速度下放
    2: (2.0, 1.1, 1.8),     # 自由: 快
    3: (2.5, 1.4, 2.5),     # 奔跑: 全杆 2.5m/s, 室内注意
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

# ---- FSM 状态机接线(P0-3 / T1.3 影子模式) ----
FSM_SHADOW = True     # 影子总开关: True = 旧逻辑照跑, DogFsm(core/fsm.py) 并行吃同样事件逐帧对照,
                      # 影子输出只比不发(不 submit / 不写 sysfs / 不打日志), 差异记 "FSM 差异" + journal;
                      # False = 完全不建影子(零行为零开销)。真机零差异验证后再进入切换任务
FSM_MODE   = "shadow" # 接线模式标记: "shadow" = 影子对照期(旧路径仍是真身);
                      # "live" = FSM 接管执行(预留给后续切换任务, 当前未实现)
