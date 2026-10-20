# -*- coding: utf-8 -*-
"""test_fsm_table.py —— DogFsm 表驱动测试(任务 T1.2 / OPTIMIZATION.md P0-3 第4条)

每个用例 = 一段事件序列 -> 期望命令序列(全量精确比对, 含 relay/gpio 去重与 log 次序),
可附 post 断言(用例跑完后检查 FSM 内部状态镜像 / 全程零 submit)。
用例相互独立: 各自新建 DogFsm(now=0), 时钟由每个事件的 now 参数显式推进,
不 sleep、不依赖真实时间、不碰硬件。

覆盖(任务点名场景全覆盖, 共 35 例):
  解锁抖动 / C 抖动 / 趴下切挡 / 起立3s内拨C / 坏帧夹在边沿中间 / 重连陈旧电平 /
  坐姿+挡位 / 冷却中摇杆 / 断链30s上锁 / 首连零指令 / 坐->趴静默 / 坐->走忽略,
  另补: 首帧电平只记录、短丢帧恢复不解锁、网页指令 RC 优先/抢占/退出趴下/damp、
  B键继电器两档去重、摇杆死区、动作前置条件(LINKDOWN/NOTSTAND)、串口重开、
  坏帧保活(sbus_ok);
  10-20 供电口事故止血: 解锁不自动趴 / C 上沿起立 / C 下沿趴下 / 2s 互锁精确边界
  (含到点重试) / A 去抖 / 起立保护期+帧重试 / 上锁下降沿不去抖。

用法: python run_test_shim.py test_fsm_table.py
(本测试只 import core.fsm + config, 不 import bridge; 垫片仅为统一入口)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.fsm import DogFsm  # noqa: E402

# ---- 通道助记(config.py: CH_ARM=4 CH_RELAY=5 CH_STAND=6 CH_GEAR=9 CH_PITCH=1) ----
IDX = {"A": 4, "B": 5, "C": 6, "G": 9, "P": 1, "R": 3, "Y": 0}
A_OFF, A_ON = 1002, 1722      # A 两档(ARM_ON=1350 分界): 1002=上锁 1722=解锁
C_DOWN, C_UP = 282, 1722      # C 两档(STAND_ON=1000 分界): 282=趴 1722=站
B_OFF, B_ON = 1002, 1722      # B 两档(RELAY_ON_RAW=1350 分界)


def ch(**over):
    """16 通道原始值。基线全 1002(A 锁/B 关/杆中/G 挡2)。
    C 必须显式给 —— 基线 1002 会被判成 C 上位, 隐式默认是坑。"""
    assert "C" in over, "frame 必须显式给 C 电平"
    c = [1002] * 16
    for k, v in over.items():
        c[IDX[k]] = v
    return c


# ---- 期望命令的构造捷径(fsm.py 输出四种: submit/relay/gpio/log) ----
def S(name, **p):
    return ("submit", name, p)


UNLOCK = ("log", "已解锁(RC)")                                    # fsm.py:318
LOCK = ("log", "已上锁(RC)")                                      # fsm.py:312
FIRST = ("log", "开机首连: 已锁定等待解锁(不发任何指令)")              # fsm.py link()
RELINK = ("log", "断链重连: 停走保持姿态, 请重新解锁")               # fsm.py:258
RELOCK = ("log", "链路重连, 已回上锁态(保持站立), 请重新解锁")        # fsm.py:304
FS30 = ("log", "RC 失联 30s, 上锁停走(保持姿态, 09-29 起不再趴下)")   # fsm.py:381
M0 = ("log", "模式 -> 趴")
M1 = ("log", "模式 -> 站")
M2 = ("log", "模式 -> 走")
M3 = ("log", "模式 -> 坐")


def GEAR(g, gait):
    return ("log", "挡位 -> %d (%s)" % (g, gait))                  # fsm.py:186


def TRICK(n, d):
    return ("log", "动作 -> %s (%ds)" % (n, d))                    # fsm.py:204


STOP = S("StopMove")
REL1050 = ("relay", 1050)
REL1950 = ("relay", 1950)
GON = ("gpio", True)
GOFF = ("gpio", False)

# 常用前奏: 开机首连(狗自站, mode=1) + 锁定期首帧(C 在上) + 解锁进走模式(挡2)
# 输出恒为 [FIRST, REL1050, UNLOCK, STOP, M2, S("FreeWalk"), GEAR(2, "FreeWalk")]
WALK2_PRELUDE = [
    ("link", 1),
    ("frame", {"A": A_OFF, "C": C_UP}, 0, 0, 0.05),
    ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 0.10),
]
WALK2_EXPECT_HEAD = [FIRST, REL1050, UNLOCK, STOP, M2,
                     S("FreeWalk"), GEAR(2, "FreeWalk")]

CASES = [
    # 1. 首帧 A 已在高位: 只记录电平不解锁(fsm.py:306-307), 后续翻拨边沿才解锁
    {"name": "first_frame_high_records_only",
     "events": [
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 0.00),
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 0.05),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 0.10),
     ],
     "expect": [REL1050, UNLOCK],
     "post": [("attr", "armed", True), ("attr", "mode", 0)]},

    # 2. 解锁抖动: A 翻拨解锁一次后连发/抖动高电平不得重复解锁;
    #    A 回落即上锁(趴着的狗 mode 已 0, 无 StandDown 可发)
    {"name": "arm_chatter_unlocks_once",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 0.00),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 0.05),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 0.10),
         ("frame", {"A": 1718, "C": C_DOWN}, 0, 0, 0.15),   # 抖动值仍 >1350
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 0.20),
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 0.25),
     ],
     "expect": [REL1050, UNLOCK, LOCK],
     "post": [("attr", "armed", False)]},

    # 3. C 抖动(未解锁): 上锁期 C 电平只跟 stand_prev, 一条模式指令都不发
    {"name": "c_chatter_while_locked_no_cmds",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 0.00),
         ("frame", {"A": A_OFF, "C": C_UP}, 0, 0, 0.05),
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 0.10),
         ("frame", {"A": A_OFF, "C": C_UP}, 0, 0, 0.15),
     ],
     "expect": [REL1050],
     "post": [("attr", "mode", 0), ("attr", "armed", False),
              ("attr", "stand_prev", True)]},

    # 4. C 抖动(已解锁, 10-20 互锁后语义): 下->上边沿起立(进程 5s 保护期外放行);
    #    2s 内反向(趴下)请求全部被互锁拦下(无输出, lie_req 挂起), C 回上则撤销
    {"name": "c_chatter_reverse_blocked_by_interlock",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 5.00),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 5.25),
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 5.50),   # C 下->上: StandUp(首个大动作, 无锚点放行)
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 5.75),   # 0.25s<2s: 互锁拦(lie_req 挂起)
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 6.00),    # C 回上: 撤销 lie_req; 起立请求
         #                                    # 幂等清标志(mode 仍 2, 无输出)
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 6.25),   # 0.75s<2s: 又拦
         ("tick", 6.50),                          # 起立流程中(rising), 3s 兜底未到点
     ],
     "expect": [REL1050, UNLOCK,
                STOP, S("StandUp"), M2, S("FreeWalk"), GEAR(2, "FreeWalk"),
                # 5.75 C 下: 上锁/趴下永不拦 -> 立即 StopMove+StandDown
                STOP, S("StandDown"), ("log", "模式 -> 趴")],
     "post": [("attr", "mode", 0), ("attr", "rising", False), ("attr", "pending", []),
              ("attr", "stand_req", False), ("attr", "lie_req", False)]},

    # 5. 趴下切挡: 只记账不发步态(fsm.py:335-339, 10-10 弹起 bug);
    #    C 拨上起立那一刻挡位补发(起立在进程 5s 保护期外, 10-20)
    {"name": "gear_while_prone_bookkeep_only",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN, "G": 282}, 0, 0, 5.00),
         ("frame", {"A": A_ON, "C": C_DOWN, "G": 282}, 0, 0, 5.25),
         ("frame", {"A": A_ON, "C": C_DOWN, "G": 1722}, 0, 0, 5.50),
         ("frame", {"A": A_ON, "C": C_DOWN, "G": 1002}, 0, 0, 5.75),
         ("frame", {"A": A_ON, "C": C_UP, "G": 1002}, 0, 0, 6.00),
     ],
     "expect": [REL1050, UNLOCK,
                STOP, S("StandUp"), M2, S("FreeWalk"), GEAR(2, "FreeWalk")],
     "post": [("attr", "mode", 2), ("attr", "gear", 2),
              ("attr", "last_gear", 2), ("attr", "gear_sent", 2)]},

    # 6. 起立 3 秒内拨 C 趴下(10-20 互锁): 2s 内反向请求被拦(lie_req 保留),
    #    每帧重试到点(距 StandUp >= 2s)自动趴下, 趴下作废 pending,
    #    迟到的 BalanceStand 不得发给趴着的狗(fsm.py:135-137, 10-17 实测事故)
    {"name": "standup_then_c_down_within_3s_cancels_pending",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN, "G": 282}, 0, 0, 5.00),
         ("frame", {"A": A_ON, "C": C_DOWN, "G": 282}, 0, 0, 5.25),
         ("frame", {"A": A_ON, "C": C_UP, "G": 282}, 0, 0, 5.50),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 6.50),   # 1.0s<2s: 互锁拦, lie_req 保留
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 7.75),   # 重试: 2.25s 到点放行, 趴下
         ("tick", 10.00),
     ],
     "expect": [REL1050, UNLOCK,
                STOP, S("StandUp"), M2, S("EconomicGait"), GEAR(1, "EconomicGait"),
                STOP, S("StandDown"), M0],
     "post": [("attr", "mode", 0), ("attr", "pending", []),
              ("attr", "lie_req", False)]},

    # 7. 起立不被打断: 3s 后 BalanceStand 照发(帧内 drain 在最前, fsm.py:291)
    {"name": "standup_uninterrupted_balance_follows",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN, "G": 282}, 0, 0, 5.00),
         ("frame", {"A": A_ON, "C": C_DOWN, "G": 282}, 0, 0, 5.25),
         ("frame", {"A": A_ON, "C": C_UP, "G": 282}, 0, 0, 5.50),
         ("frame", {"A": A_ON, "C": C_UP, "G": 282}, 0, 0, 6.00),
         ("frame", {"A": A_ON, "C": C_UP, "G": 282}, 0, 0, 8.75),
     ],
     "expect": [REL1050, UNLOCK,
                STOP, S("StandUp"), M2, S("EconomicGait"), GEAR(1, "EconomicGait"),
                S("BalanceStand")],
     "post": [("attr", "mode", 2), ("attr", "pending", [])]},

    # 8. 坏帧夹在 A 边沿中间: 坏帧不作控制输入, prev_high 不被污染,
    #    之后干净帧的翻拨边沿照常解锁; 坏帧本身只刷 last_any(链路活着)
    {"name": "bad_frame_between_a_edges",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 0.00),
         ("bad", 0.05),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 0.10),
         ("bad", 0.15),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 0.20),
     ],
     "expect": [REL1050, UNLOCK],
     "post": [("attr", "armed", True), ("attr", "prev_high", True)]},

    # 9. 坏帧夹在 C 边沿中间: 坏帧不碰 stand_prev, 边沿起立照常; 另锁定两条
    #    附带语义 —— StandUp 后 pending 未清期间摇杆被吞(fsm.py:216),
    #    BalanceStand 一发杆量立即恢复
    {"name": "bad_frame_between_c_edges_pending_blocks",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 5.00),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 5.25),
         ("bad", 5.30),
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 5.50),
         ("frame", {"A": A_ON, "C": C_UP, "P": 1722}, 0, 0, 5.75),
         ("bad", 6.00),
         ("tick", 8.75),
         ("frame", {"A": A_ON, "C": C_UP, "P": 1722}, 0, 0, 9.00),
     ],
     "expect": [REL1050, UNLOCK,
                STOP, S("StandUp"), M2, S("FreeWalk"), GEAR(2, "FreeWalk"),
                S("BalanceStand"),
                S("Move", x=-2.0, y=0.0, z=0.0)],
     "post": [("attr", "moving", True), ("attr", "armed", True)]},

    # 9b. 运动中坏帧急停: 坏帧 hold 幂等(只发一次 StopMove), 干净帧回中不补停
    #     (走 link 首连 1->2 同步路径起立, 无 pending 干扰)
    {"name": "bad_frame_while_moving_stops",
     "events": WALK2_PRELUDE + [
         ("frame", {"A": A_ON, "C": C_UP, "P": 1722}, 0, 0, 0.15),
         ("bad", 0.20),
         ("bad", 0.21),
         ("frame", {"A": A_ON, "C": C_UP, "P": 1002}, 0, 0, 0.25),
     ],
     "expect": list(WALK2_EXPECT_HEAD) + [
         S("Move", x=-2.0, y=0.0, z=0.0),
         STOP],
     "post": [("attr", "moving", False), ("attr", "armed", True)]},

    # 10. 重连陈旧电平: 走动中断链重连(link gen2) -> 重连即 StopMove + 下帧回上锁;
    #     A 停在高位(陈旧电平)不得自行再解锁, 必须重新翻拨
    {"name": "relink_stale_levels_no_self_rearm",
     "events": [
         ("link", 1),
         ("frame", {"A": A_OFF, "C": C_UP, "G": 1722, "B": B_ON}, 0, 0, 0.05),
         ("frame", {"A": A_ON, "C": C_UP, "G": 1722, "B": B_ON}, 0, 0, 0.10),
         ("frame", {"A": A_ON, "C": C_UP, "G": 1722, "B": B_ON, "P": 1722}, 0, 0, 0.20),
         ("link", 2),
         ("setlink", False),
         ("frame", {"A": A_ON, "C": C_UP, "G": 1722, "B": B_ON}, 0, 0, 0.40),
         ("frame", {"A": A_ON, "C": C_UP, "G": 1722, "B": B_ON}, 0, 0, 0.50),
         ("frame", {"A": A_OFF, "C": C_UP, "G": 1722, "B": B_ON}, 0, 0, 0.60),
         ("frame", {"A": A_ON, "C": C_UP, "G": 1722, "B": B_ON}, 0, 0, 1.50),  # 距解锁 1.4s(过 A 去抖)
     ],
     "expect": [FIRST, REL1950, GON,
                UNLOCK, STOP, M2, S("TrotRun"), GEAR(3, "TrotRun"),
                S("Move", x=-2.5, y=0.0, z=0.0),
                STOP, RELINK,
                RELOCK,
                UNLOCK],
     "post": [("attr", "armed", True), ("attr", "mode", 2),
              ("attr", "link_gen_cur", 2)]},

    # 11. 坐姿+挡位: 坐下(mode3)后切挡 -> 挡位照发(现行语义: 判据是 mode!=0,
    #     mode3 不是趴, fsm.py:337 逐行等价 bridge.py:654); C 在上的帧每帧先出
    #     StopMove(3->2 被忽略但 StopMove 已入列, fsm.py:147+158-160)
    {"name": "sit_then_gear_change_passthrough",
     "events": WALK2_PRELUDE + [
         ("trick", "Sit", 3, 0.15, "OK"),
         ("frame", {"A": A_ON, "C": C_UP, "G": 1722}, 0, 0, 0.20),
     ],
     "expect": WALK2_EXPECT_HEAD + [
         S("Sit"), TRICK("Sit", 3), M3,
         STOP, S("TrotRun"), GEAR(3, "TrotRun")],
     "post": [("attr", "mode", 3), ("attr", "gear", 3), ("attr", "gear_sent", 3)]},

    # 12. 坐->趴静默: 狗已在低位, 3->0 只记状态不发任何指令(fsm.py:138-146)
    {"name": "sit_to_down_silent",
     "events": WALK2_PRELUDE + [
         ("trick", "Sit", 3, 0.15, "OK"),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 0.20),
     ],
     "expect": WALK2_EXPECT_HEAD + [
         S("Sit"), TRICK("Sit", 3), M3,
         M0],
     "post": [("attr", "mode", 0)]},

    # 13. 坐->走忽略: 坐着 C 在上位不算数, 模式保持 3, 不发 StandUp/无"模式->走"
    #     (fsm.py:157-160); 每帧只有那条先入列的 StopMove
    {"name": "sit_c_up_ignored_mode_stays",
     "events": WALK2_PRELUDE + [
         ("trick", "Sit", 3, 0.15, "OK"),
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 0.20),
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 0.25),
     ],
     "expect": WALK2_EXPECT_HEAD + [
         S("Sit"), TRICK("Sit", 3), M3,
         STOP, STOP],
     "post": [("attr", "mode", 3)]},

    # 14. 坐后完整回环: C 拨下(静默趴) -> C 下->上边沿才真正起立(过程 5s 保护期外,
    #     10-20) -> 3s 后补 BalanceStand
    {"name": "sit_down_then_c_edge_standup",
     "events": WALK2_PRELUDE + [
         ("trick", "Sit", 3, 0.15, "OK"),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 5.25),
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 5.50),
         ("tick", 8.75),
     ],
     "expect": WALK2_EXPECT_HEAD + [
         S("Sit"), TRICK("Sit", 3), M3,
         M0,
         STOP, S("StandUp"), M2,
         S("BalanceStand")],
     "post": [("attr", "mode", 2)]},

    # 15. 冷却中摇杆屏蔽: 动作播放期 Move 被吞、新动作 NOTSTAND;
    #     冷却一过杆量立即生效, 回中出 StopMove(fsm.py:198/216)
    {"name": "cooldown_blocks_sticks_and_tricks",
     "events": WALK2_PRELUDE + [
         ("trick", "Hello", 4, 0.15, "OK"),
         ("frame", {"A": A_ON, "C": C_UP, "P": 1722}, 0, 0, 0.20),
         ("trick", "Dance1", 24, 0.25, "NOTSTAND"),
         ("frame", {"A": A_ON, "C": C_UP, "P": 1722}, 0, 0, 4.20),
         ("frame", {"A": A_ON, "C": C_UP, "P": 1002}, 0, 0, 4.30),
     ],
     "expect": WALK2_EXPECT_HEAD + [
         S("Hello"), TRICK("Hello", 4),
         S("Move", x=-2.0, y=0.0, z=0.0),
         STOP],
     "post": [("attr", "mode", 2), ("attr", "action", "Hello"),
              ("attr", "moving", False)]},

    # 16. 断链 30s 上锁: 0.25s 一过先安全关断继电器+停走; 30s 才上锁,
    #     全程不趴下、不解锁前也无 StandDown(09-29 口径, fsm.py:376-384)
    {"name": "failsafe_30s_disarm_keeps_posture",
     "events": [
         ("link", 1),
         ("frame", {"A": A_OFF, "C": C_UP, "G": 1722, "B": B_ON}, 0, 0, 0.05),
         ("frame", {"A": A_ON, "C": C_UP, "G": 1722, "B": B_ON}, 0, 0, 0.10),
         ("frame", {"A": A_ON, "C": C_UP, "G": 1722, "B": B_ON, "P": 1722}, 0, 0, 0.20),
         ("safety", 2.85, 3.05),
         ("tick", 3.05),
         ("safety", 9.80, 10.00),
         ("safety", 30.80, 31.00),
         ("tick", 31.00),
     ],
     "expect": [FIRST, REL1950, GON,
                UNLOCK, STOP, M2, S("TrotRun"), GEAR(3, "TrotRun"),
                S("Move", x=-2.5, y=0.0, z=0.0),
                REL1050, GOFF, STOP,
                FS30],
     "post": [("attr", "armed", False), ("attr", "mode", 2),
              ("attr", "moving", False), ("attr", "sbus_ok", False),
              ("attr", "sbus_clean", False)]},

    # 17. 短丢帧恢复: gap 0.4s 只停走, 不上锁不趴; 干净帧回来后仍处解锁态
    {"name": "short_gap_stops_but_stays_armed",
     "events": [
         ("link", 1),
         ("frame", {"A": A_OFF, "C": C_UP}, 0, 0, 0.05),
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 0.10),
         ("frame", {"A": A_ON, "C": C_UP, "P": 1722}, 0, 0, 0.20),
         ("safety", 0.40, 0.60),
         ("frame", {"A": A_ON, "C": C_UP, "P": 1002}, 0, 0, 0.65),
         ("safety", 0.00, 0.65),
     ],
     "expect": list(WALK2_EXPECT_HEAD) + [
         S("Move", x=-2.0, y=0.0, z=0.0),
         STOP],
     "post": [("attr", "armed", True), ("attr", "sbus_ok", True),
              ("attr", "sbus_clean", True)]},

    # 18. 首连零指令: 开机首连一条狗指令都不发(狗自己起立, fsm.py:248-252),
    #     上锁期帧也只有首帧继电器初始化一条输出
    {"name": "first_connect_zero_submits",
     "events": [
         ("link", 1),
         ("frame", {"A": A_OFF, "C": C_UP}, 0, 0, 0.05),
         ("frame", {"A": A_OFF, "C": C_UP}, 0, 0, 0.10),
     ],
     "expect": [FIRST, REL1050],
     "post": [("attr", "mode", 1), ("nosubmit",)]},

    # 19. RC 解锁时网页指令全忽略(axes 仍记账); 模式不被网页改动
    {"name": "web_cmds_ignored_while_rc_armed",
     "events": WALK2_PRELUDE + [
         ("web", "arm", 0.15),
         ("web", "stand", 0.16),
         ("web", "disarm", 0.17),
         ("webaxes", 1.0, 0.0, 0.0, 0.18),
         ("tick", 0.19),
     ],
     "expect": list(WALK2_EXPECT_HEAD),
     "post": [("attr", "mode", 2), ("attr", "armed", True),
              ("attr", "web_armed", False), ("attr", "web_vx", 1.0)]},

    # 20. RC 解锁抢占网页: 网页正在开车(web tick 出 Move), RC 一解锁网页即让位
    #     (首连 mode=1, web arm 本就不发指令); 解锁帧 C 在下不再自动趴下
    #     (10-20 删除"armed 且 C 在下每帧趴"路径), 解锁只出一次回中停走, 模式保持 2
    {"name": "rc_unlock_takes_over_web",
     "events": [
         ("link", 1),
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 5.00),
         ("web", "arm", 5.25),
         ("web", "walk", 5.50),
         ("webaxes", 0.8, 0.0, 0.0, 5.75),
         ("tick", 6.00),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 6.25),
         ("tick", 6.50),
     ],
     "expect": [FIRST, REL1050,
                STOP, M2,
                S("Move", x=0.4, y=0.0, z=0.0),
                UNLOCK, STOP],
     "post": [("attr", "armed", True), ("attr", "web_armed", False),
              ("attr", "mode", 2)]},

    # 21. 网页会话闭环: arm(趴->站: StandUp+挂3s) -> walk(清 pending, 1->2 无 StandUp)
    #     -> axes+tick 驱动 Move -> ws closed 自动趴下(距 StandUp >= 2s 过互锁, 10-20),
    #     网页控制解除
    {"name": "web_session_drive_then_close_lies_down",
     "events": [
         ("web", "arm", 5.25),
         ("web", "walk", 5.50),
         ("webaxes", 0.5, 0.0, 0.0, 5.75),
         ("tick", 6.00),
         ("web", "closed", 7.75),
         ("tick", 8.00),
     ],
     "expect": [STOP, S("StandUp"), M1,
                STOP, M2,
                S("Move", x=0.25, y=0.0, z=0.0),
                STOP, S("StandDown"), M0],
     "post": [("attr", "web_armed", False), ("attr", "mode", 0),
              ("attr", "pending", [])]},

    # 22. 网页 damp: 与 disarm 同径趴下(距 StandUp >= 2s 过互锁), 且清掉 arm 挂的 pending
    {"name": "web_damp_lies_down_and_clears_pending",
     "events": [
         ("web", "arm", 5.25),
         ("web", "damp", 7.50),
         ("tick", 10.00),
     ],
     "expect": [STOP, S("StandUp"), M1,
                STOP, S("StandDown"), M0],
     "post": [("attr", "mode", 0), ("attr", "web_armed", False),
              ("attr", "pending", [])]},

    # 23. B 键继电器两档去重: 首帧必写(记忆从 None 起), 电平不变不重发
    {"name": "relay_b_key_dedup",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN, "B": B_OFF}, 0, 0, 0.00),
         ("frame", {"A": A_OFF, "C": C_DOWN, "B": B_ON}, 0, 0, 0.05),
         ("frame", {"A": A_OFF, "C": C_DOWN, "B": B_ON}, 0, 0, 0.10),
         ("frame", {"A": A_OFF, "C": C_DOWN, "B": 282}, 0, 0, 0.15),
         ("frame", {"A": A_OFF, "C": C_DOWN, "B": 282}, 0, 0, 0.20),
     ],
     "expect": [REL1050, REL1950, GON, REL1050, GOFF],
     "post": [("attr", "_relay_us", 1050), ("attr", "_gpio_lv", False)]},

    # 24. 摇杆死区: 起立 pending 排空后, 死区内(1040)不出车; 满杆连续帧逐帧出
    #     Move(指令流); 回中只出一次 StopMove; BalanceStand 由 tick 到点补发
    #     (起立在进程 5s 保护期外, 10-20)
    {"name": "stick_deadzone_and_move_stream",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 5.00),
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 5.25),
         ("frame", {"A": A_ON, "C": C_UP, "P": 1040}, 0, 0, 5.50),
         ("tick", 8.50),
         ("frame", {"A": A_ON, "C": C_UP, "P": 1040}, 0, 0, 8.75),
         ("frame", {"A": A_ON, "C": C_UP, "P": 282}, 0, 0, 9.00),
         ("frame", {"A": A_ON, "C": C_UP, "P": 282}, 0, 0, 9.25),
         ("frame", {"A": A_ON, "C": C_UP, "P": 1002}, 0, 0, 9.50),
     ],
     "expect": [REL1050, UNLOCK,
                STOP, S("StandUp"), M2, S("FreeWalk"), GEAR(2, "FreeWalk"),
                S("BalanceStand"),
                S("Move", x=2.0, y=0.0, z=0.0),
                S("Move", x=2.0, y=0.0, z=0.0),
                STOP],
     "post": [("attr", "moving", False)]},

    # 25. 动作前置条件: 链路断 LINKDOWN(不收冷却不认账); 非走模式 NOTSTAND;
    #     走模式 OK; Sit 后(mode3)再触发 NOTSTAND
    {"name": "trick_gating_linkdown_notstand",
     "events": [
         ("trick", "Hello", 4, 0.00, "LINKDOWN"),
         ("link", 1),
         ("trick", "Hello", 4, 0.05, "NOTSTAND"),
         ("frame", {"A": A_OFF, "C": C_UP}, 0, 0, 0.10),
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 0.15),
         ("trick", "Sit", 3, 0.20, "OK"),
         ("trick", "Hello", 4, 0.25, "NOTSTAND"),
     ],
     "expect": WALK2_EXPECT_HEAD + [S("Sit"), TRICK("Sit", 3), M3],
     "post": [("attr", "link_up", True), ("attr", "mode", 3)]},

    # 26. 串口重开: 强制回上锁态、首帧只记录、gear 展示清 None;
    #     重开后拨回同挡 g==last_gear 不更新展示(fsm.py:267-274 原语义保留);
    #     重新翻拨解锁距上次解锁边沿 1.45s(过 A 去抖, 10-20)
    {"name": "serial_reopen_requires_rearm",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN, "G": 1722}, 0, 0, 0.00),
         ("frame", {"A": A_ON, "C": C_DOWN, "G": 1722}, 0, 0, 0.05),
         ("serial",),
         ("frame", {"A": A_ON, "C": C_DOWN, "G": 1722}, 0, 0, 0.15),
         ("frame", {"A": A_OFF, "C": C_DOWN, "G": 1722}, 0, 0, 1.25),
         ("frame", {"A": A_ON, "C": C_DOWN, "G": 1722}, 0, 0, 1.50),
     ],
     "expect": [REL1050, UNLOCK, UNLOCK],
     "post": [("attr", "armed", True), ("attr", "gear", None),
              ("attr", "last_gear", 3)]},

    # 27. 坏帧保活: 坏帧刷 last_any, 1s 内 sbus_ok 仍真(链路活着只是脏),
    #     gap>0.25 停走但 30s 内不上锁(fsm.py:374, 380)
    {"name": "bad_frame_keeps_link_alive",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 0.00),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 0.05),
         ("bad", 0.80),
         ("safety", 1.15, 1.20),
     ],
     "expect": [REL1050, UNLOCK],
     "post": [("attr", "sbus_ok", True), ("attr", "sbus_clean", False),
              ("attr", "armed", True)]},

    # ---- 10-20 供电口事故止血用例(任务 8 点名场景) ----

    # 28. 解锁不自动做任何动作: 站着的狗(首连 mode=1)解锁帧 C 在下,
    #     旧"armed 且 C 在下每帧趴"路径已删 —— 无 StandDown, 模式保持
    {"name": "unlock_does_nothing_mode_kept",
     "events": [
         ("link", 1),
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 5.00),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 5.25),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 5.50),
     ],
     "expect": [FIRST, REL1050, UNLOCK],
     "post": [("attr", "mode", 1), ("attr", "armed", True),
              ("attr", "stand_req", False), ("attr", "lie_req", False)]},

    # 29. C 上沿起立: 趴着解锁后 C 下->上边沿才起立(保护期外), 稳态帧补同步幂等无输出
    {"name": "c_up_edge_stands_up",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 5.00),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 5.25),
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 5.50),
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 5.75),
     ],
     "expect": [REL1050, UNLOCK,
                STOP, S("StandUp"), M2, S("FreeWalk"), GEAR(2, "FreeWalk")],
     "post": [("attr", "mode", 2), ("attr", "stand_req", False)]},

    # 30. C 下沿趴下: 走模式下 C 上->下边沿才趴下(首例大动作无锚点放行),
    #     趴下作废起立挂的 pending
    {"name": "c_down_edge_lies_down",
     "events": WALK2_PRELUDE + [
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 5.25),
         ("tick", 10.00),
     ],
     "expect": WALK2_EXPECT_HEAD + [
         STOP, S("StandDown"), M0],
     "post": [("attr", "mode", 0), ("attr", "pending", []), ("attr", "lie_req", False)]},

    # 31. 2s 互锁精确边界: 起立(5.50)后 6.00 反向被拦(0.50s), 整 2.00s(7.50,
    #     不小于 MODE_DWELL_S)重试帧放行
    {"name": "interlock_exact_dwell_boundary",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 5.00),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 5.25),
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 5.50),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 6.00),   # 0.50s<2s: 拦, 零输出
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 7.50),   # 整 2.0s: 放行
     ],
     "expect": [REL1050, UNLOCK,
                STOP, S("StandUp"), M2, S("FreeWalk"), GEAR(2, "FreeWalk"),
                STOP, S("StandDown"), M0],
     "post": [("attr", "mode", 0)]},

    # 32. A 去抖: 解锁上升沿距上次解锁边沿 < ARM_DEBOUNCE_S(1s) 忽略
    #     (边沿仍刷新锚点, 稳定 1s 后的翻拨才解锁)
    {"name": "arm_debounce_ignores_quick_rearm",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 5.00),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 5.25),   # 解锁(锚点 5.25)
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 5.75),   # 上锁
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 6.00),   # 距 0.75s<1s: 去抖忽略(锚点 6.00)
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 6.50),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 7.25),   # 距 1.25s: 解锁
     ],
     "expect": [REL1050, UNLOCK, LOCK, UNLOCK],
     "post": [("attr", "armed", True)]},

    # 33. 起立保护期: 进程启动(锚点 0)后 5s 内会发 StandUp 的请求全拦(零输出),
    #     stand_req 保留每帧重试, 5s 过后自动起立 —— 不会因边沿只来一次而丢请求
    {"name": "stand_guard_blocks_then_frame_retry",
     "events": [
         ("frame", {"A": A_OFF, "C": C_DOWN}, 0, 0, 1.00),
         ("frame", {"A": A_ON, "C": C_DOWN}, 0, 0, 1.25),
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 1.50),   # C 上沿: 保护期拦
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 2.00),   # 重试: 仍拦
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 5.25),   # 重试: 5s 过, 放行
     ],
     "expect": [REL1050, UNLOCK,
                STOP, S("StandUp"), M2, S("FreeWalk"), GEAR(2, "FreeWalk")],
     "post": [("attr", "mode", 2), ("attr", "stand_req", False)]},

    # 34. 上锁下降沿不被去抖: 解锁 0.5s 后即上锁(远小于 A 去抖窗),
    #     照常执行趴下收权(2->0 首例大动作无锚点放行)
    {"name": "lock_edge_never_debounced",
     "events": [
         ("link", 1),
         ("frame", {"A": A_OFF, "C": C_UP}, 0, 0, 5.00),
         ("frame", {"A": A_ON, "C": C_UP}, 0, 0, 5.25),
         ("frame", {"A": A_OFF, "C": C_UP}, 0, 0, 5.75),
     ],
     "expect": [FIRST, REL1050, UNLOCK,
                STOP, M2, S("FreeWalk"), GEAR(2, "FreeWalk"),
                STOP, S("StandDown"), M0, LOCK],
     "post": [("attr", "armed", False), ("attr", "mode", 0)]},
]


def _first_diff(expect, got):
    for i in range(max(len(expect), len(got))):
        e = expect[i] if i < len(expect) else "<缺少>"
        g = got[i] if i < len(got) else "<多出>"
        if e != g:
            return i, e, g
    return None


def run_case(c):
    f = DogFsm(now=0.0)
    got = []
    for ev in c["events"]:
        k = ev[0]
        if k == "frame":
            got += f.frame(ch(**ev[1]), ev[2], ev[3], now=ev[4])
        elif k == "bad":                      # 坏帧: 通道值不作控制输入, 给基线即可
            got += f.frame([1002] * 16, 1, 0, now=ev[1])
        elif k == "tick":
            got += f.tick(now=ev[1])
        elif k == "safety":
            got += f.safety(ev[1], now=ev[2])
        elif k == "link":
            got += f.link(ev[1])
        elif k == "setlink":
            f.set_link(ev[1])
        elif k == "web":
            got += f.web_cmd(ev[1], now=ev[2])
        elif k == "webaxes":
            got += f.web_cmd("axes", vx=ev[1], vy=ev[2], vyaw=ev[3], now=ev[4])
        elif k == "trick":
            cmds, res = f.trick(ev[1], ev[2], now=ev[3])
            assert res == ev[4], "%s: trick(%s) 返回 %r, 期望 %r" % (
                c["name"], ev[1], res, ev[4])
            got += cmds
        elif k == "serial":
            f.serial_reopened()
        else:
            raise AssertionError("%s: 未知事件种类 %r" % (c["name"], k))
    if got != c["expect"]:
        i, e, g = _first_diff(c["expect"], got)
        raise AssertionError(
            "%s: 命令序列不符(首个差异 #%d)\n  期望: %r\n  实际: %r\n  差异: 期望 %r / 实际 %r"
            % (c["name"], i, c["expect"], got, e, g))
    for p in c.get("post", []):
        if p[0] == "attr":
            val = getattr(f, p[1])
            assert val == p[2], "%s: post %s = %r, 期望 %r" % (
                c["name"], p[1], val, p[2])
        elif p[0] == "nosubmit":
            assert not any(x[0] == "submit" for x in got), \
                "%s: 全程不应有任何 submit" % c["name"]
        else:
            raise AssertionError("%s: 未知 post 种类 %r" % (c["name"], p[0]))


def main():
    for c in CASES:
        run_case(c)
        print("ok  %-42s %d 事件 / %d 命令" % (c["name"], len(c["events"]),
                                               len(c["expect"])))
    print("test_fsm_table: %d/%d 用例通过" % (len(CASES), len(CASES)))


if __name__ == "__main__":
    main()
