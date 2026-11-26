# GO2 控制系统架构总览（2026-10-31，ver 975b7a6+）

> 本文是当日快照版架构图：现状树 + 数据流 + 规划 + 多 agent 并行可行性。
> 正史文档仍是 `ARCHITECTURE.md`（现状+目标）与 `OPTIMIZATION.md`（改造清单）；
> 本文侧重"此刻长什么样、接下来怎么并行开发"。

> **【2026-10-31 深夜更新】** 早间快照后当晚已落地三件事（HEAD `b2fd0c5`）：
> ① **bridge.py 拆分**：2473→454 行，平铺四模块 stats.py / telemetry.py / link.py / webserver.py（11-03 增 gas.py 气体传感器读取, 非安全件）/
>    webserver.py（merge `9a6deae`，每步行为零变更）；
> ② **旧控制路径删除**：Dog / FsmShadow / _RecLink / _RecRelay / rc_loop 旧体等已删
>    （`f80be4f`，提前于"active 稳定 1-2 周"门槛，用户拍板，ADR-0004 后续记录；
>    回退锚点 tag `pre-split-20261031`）；FsmActive 是唯一控制器；
> ③ **测试重定向**：8 套仿真测试改走现役 active 路径、shadow 用例退役；新增
>    test_deps.py（依赖守门）+ test_release_files.py（发布校验），现 **17 套**
>    （test_pa_v3 板端除外）。
> 下文「一、系统分层树」的影子路径节点与测试行、「四、待办规划」第 1/2 条已按此就地更新；
> 其余章节（含"待办规划"其余条目与第五节分析）保留早间快照原貌，与深夜现状冲突处以本横幅为准。

---

## 一、系统分层树

```
GO2 控制系统
│
├── 硬件层
│   ├── 宇树 Go2 狗本体 ── 192.168.123.161（WebRTC :9991, con_notify 局域网信令）
│   │     ├── 关节电机 ×12（lowstate: q 角度 + 温度；无电流/电压上报）
│   │     ├── BMS 电池（lowstate.bms_state: soc/bq_ntc/mcu_ntc/cycle）
│   │     ├── 运动服务（sportmodestate: mode/高度/步态/速度/姿态角/错误码/position）
│   │     └── 喇叭（音频 hub）与可选云台
│   ├── 核桃派 WalnutPi（桥主机）── 家 WiFi 192.168.8.53 / 狗侧有线 192.168.123.x
│   │     ├── /dev/ttyS2 串口 ← SBUS 接收机（实体遥控 100fps 全时在线）
│   │     ├── pwm1+pwm2（继电器双路并出, B 键 1950/1050us）+ 4 路 GPIO
│   │     └── systemd：go2-audio（先起）→ go2-bridge（部署顺序同）
│   └── Skydroid G12 遥控器（实体遥控 + 内置安卓屏 = App 宿主, adb 15be5f38）
│
├── 板上服务层（Python, 无第三方重依赖; 部署清单由 deploy_pc.collect_files 自动收集）
│   │
│   ├── go2-bridge（主进程, FastAPI/uvicorn :8000）
│   │   ├── 【执行核心】FSM 路径 ★现役（config.FSM_MODE="active", 10-31 正式替换）
│   │   │   ├── FsmActive._sync —— rc_loop 每帧喂 DogFsm, 唯一有下发权
│   │   │   └── DogFsm(core/fsm.py) —— 解锁/上锁、C 站趴、起立流程(rising+兜底
│   │   │        BalanceStand)、2s 互锁、3s 起立保护、挡位步态、失控保护、
│   │   │        换源播种 guard、网页指令(arm/closed 等)、checked-submit 探针
│   │   ├── 【旧路径已删除】（10-31 深夜, f80be4f + 9a6deae 合并）: Dog/FsmShadow/
│   │   │     rc_loop 旧体不复存在, FsmActive 是唯一控制器; 回退锚点 tag pre-split-20261031
│   │   │     （config.FSM_MODE/FSM_SHADOW 暂留无读取方, 下版本移除）
│   │   ├── 【输入前端】
│   │   │   ├── sbus.py —— 串口解码 → 16 通道原始值; SbusStats(60s 秒桶趋势/丢包/告警)
│   │   │   └── netrc.py —— UDP :15010 ← App 20Hz 全通道 JSON（新鲜 300ms 优先;
│   │   │         换源首帧只记电平不合成边沿; languard 令牌/白名单可选）
│   │   ├── 【遥测管线】（只读, 不碰控制）
│   │   │   ├── DogLink（WebRTC 会话线程: 永远重连/指数退避/fd 安全关闭/半死拆会话）
│   │   │   ├── 订阅: rt/sportmodestate + rt/lf/sportmodestate → 姿态/高度(控制决策也用)
│   │   │   ├── 订阅: rt/lf/lowstate → 关节温度×12+电池(GO2信息卡) [bms 有 -99x 哨兵]
│   │   │   ├── 订阅: rt/lf/mainboardstate(此固件不发) + PROBE_TOPICS 探测组(首包诊断)
│   │   │   └── /state "dog"/"dog2" 段 ← state 镜像（GIL 单属性赋值即原子）
│   │   ├── 【网页服务】uvicorn :8000
│   │   │   ├── /sbus 调试台(5 页 SPA: 仪表盘/通道/日志/设置+关于) / 遥控页 / 旧版
│   │   │   ├── /ws 实时推送(0.5s) + 指令通道(mode/axes, 缺键不清零) + ?tok= 鉴权
│   │   │   ├── /state 全量快照(诊断地基) / /logs 内存环600行 / /journal 黑匣子读取
│   │   │   └── languard.py —— 令牌(hmac)+IP 白名单门禁(现全空=放行, 告警已按用户定案移除)
│   │   ├── 【动作网关】UDP :15011(仅回环) ← pa_recv 的 TRICK → 白名单校验 → FSM
│   │   └── 【黑匣子】journal.py —— 异步落盘 jsonl(32MB 轮转), kind=event/cmd(非Move) fsync
│   │
│   └── go2-audio（pa_recv.py 独立进程, 与桥解耦）
│       ├── :15001 控制 UDP（HELLO/VOL/PLAY/LIST/TALK/**RC 查询**）
│       │     └── RC 应答 = HTTP 读桥 /state → "RC arm mode gear soc fresh"（v48）
│       ├── :15000 音频帧(40ms) / :15003 分块上传(断点续传)
│       ├── TRICK → 桥 :15011（回包 OK/LINKDOWN/NOTSTAND 原样透传）
│       └── 每秒写 /run/go2-audio.json（桥 /state 合并 ready/音量/对讲状态）
│
├── 客户端层
│   ├── G12 App（PaSend, 纯 Java 程序化 UI, 无 gradle 构建链, APP_VERSION=48）
│   │   ├── 主屏 = WebView(调试台) + 四段底栏(喊话台/驱鸟台/动作/网络控制)
│   │   ├── 网络控制页(原生 ManualCtrlActivity)：20Hz 杆量(15010) + 探活 3s 回灌
│   │   │     （armed/walk/gear 与桥不符自动校正, 1.5s 按键宽限）+ 电池横椭圆图标
│   │   ├── PaService：15001 控制客户端 / 悬浮喊话台 / 驱鸟面板 / 开机自启
│   │   └── 更新：板上 deploy_page :15080 版本.json + 内置更新器
│   └── 任意浏览器
│       ├── 遥控页 index.html —— 手机触屏双摇杆/解锁/急停（ws 指令, 走桥 FSM）
│       └── 调试台 sbus.html —— 5 页监控+GO2信息卡+顶栏电池+低电量警告+设置(改板址)
│
└── 工程链（开发者侧）
    ├── deploy_pc.py —— 部署(上传+md5门+语法门+.bak自动回滚+变体落后硬门+EEE自检)
    │     └── collect_files(): ast 解析入口 import + static/*.html 自动清单
    ├── tools/make_release.py —— 真身 → 公开仓 Unitree-Dog-WalnutPi-Setup 快照
    │     └── 排除清单拦敏感(config_local/setup_dog2 WiFi 明文/内部文档/android-app)
    ├── 测试 17 套（python run_test_shim.py test_xxx.py, 全离线仿真零硬件; test_pa_v3 板端除外;
    │     深夜新增 test_deps 依赖守门 + test_release_files 发布校验）
    │     ├── test_fsm_table 42 例（FSM 全语义, 表驱动）
    │     ├── test_fsm_mode（active/webface 两用例; shadow 用例随旧路径删除退役）
    │     ├── sim 组：netrc/link/reconnect/unlock/heartbeat/stand_state/safety/queue
    │     └── test_languard（鉴权单元+令牌门+白名单门）/ test_journal / test_pa_stereo
    └── 文档：CLAUDE.md(约定/台账) ARCHITECTURE.md OPTIMIZATION.md TODO.md
        （CLAUDE.md/内部文档不进公开仓, make_release 排除）
```

## 二、关键数据流

```
控制流（20~100Hz）:
  G12 摇杆 ──UDP:15010──▶ netrc ─┐
  实体遥控 ──SBUS 串口──▶ sbus ──┤  新鲜源优先(300ms) → rc_loop 一帧
                                  ▼
                          FsmActive._sync → DogFsm.frame() ──submit 探针──▶ DogLink ──WebRTC──▶ 狗
                                  │                                            (入队失败不记账,下一帧重试)
  网页摇杆 ──ws axes──▶ state.web_v* ──▶ FSM tick 网页驱动(仅 RC 上锁时) ┘

安全链: 失控保护 0.25s 停走(网络源 0.55s) → 30s 失联上锁保持姿态 / 断链重连=清上锁 /
        大动作 2s 互锁(到点自动补发) / 起立保护期 3s / estop=A 键上锁沿

遥测流（1~5Hz, 只读）:
  狗 ──WebRTC 订阅──▶ _on_*_state 回调 → state 单属性镜像 → /state(/ws) → 调试台/App
  (新鲜度: dog_state_t / dog_low_t; 断狗自动 stale 灰显)

音频流: App ──:15003 上传预设──▶ 板 ~/pa-files ──:15001 PLAY──▶ pa_recv ──40ms帧 :15000──▶ 喇叭
        App ──TALK──▶ 实时对讲(与播放互斥)
```

## 三、已完成的架构里程碑（2026-10）

| 里程碑 | 结果 |
|---|---|
| FSM 两阶段迁移 | shadow 34min 真机零差异过门 → **active 正式替换**（ver 22724d4），旧路径停用（当日深夜已删除，见顶部横幅） |
| 影子期三大根因清零 | closed 停走对齐 / frame 对账同相 / 换源+上升沿 guard（42 例表驱动锁死） |
| 监控面 | GO2 信息卡（关节温度/姿态角/电池状态）+ 顶栏电池胶囊 + 低电量警告条 + 帧率趋势去假低点 |
| App v47→v48 | 接管继承(查 /state 后补发) + 探活状态回灌(断电自动跟上) + 电池图标 + 1600ms×2 超时预算 |
| 安全加固 | languard 令牌/白名单（可开可关）、部署 .bak 回滚、变体落后硬门、EEE 定案长期关闭 |
| 工程链 | 部署清单 ast 自动收集（查出历史漏 netrc.py）、公开仓快照流程 + 敏感文件排除 |

## 四、待办规划（优先级序）

1. ~~**删除旧 Dog 路径**~~ ✅ 已完成（10-31 深夜，提前于"稳定 1~2 周"门槛，用户拍板）：
   `f80be4f` 删除 + `9a6deae` 合并；删后首道真机门 = `docs/REGRESSION-CHECKLIST.md` 12 场景。
2. ~~**bridge.py 模块化拆分**~~ ✅ 已完成（10-31 深夜）：按 5.4 路线落地为平铺四模块
   stats/telemetry/link/webserver，bridge.py 2473→454 行，每步行为零变更。
3. **_deploy_dog2 变体收敛**：旧副本清理（待用户点头）+ 变体只留 config_local 差异。
4. **鉴权落地**（可选）：NETRC_TOKEN / WEB_TOKEN / PA_ALLOW 配置化启用（机制已在，只差配置）。
5. **App 上架链**：v48 走 deploy_page.py 上架 15080（等用户验收点头）。

## 五、多 agent 并行优化可行性（专业评估）

### 5.1 并行切面评分

| 工作包 | 代码独立度 | 测试闭环 | 真机依赖 | 并行可行性 |
|---|---|---|---|---|
| A. FSM 演进（core/fsm.py + test_fsm_table） | ★★★ 独立模块 | ★★★ 表驱动全离线 | 仅验收阶段 | **高** |
| B. 网页前端（sbus/index.html） | ★★★ 纯静态+契约 | ★★ 灌真值截图验收 | 零 | **高** |
| C. App 原生（android-app） | ★★★ 独立构建链 | ★★ adb 真机验收 | 装机验收阶段 | **高** |
| D. 遥测/监控扩展（订阅+state+卡） | ★★ 与 bridge.py 同文件 | ★★ /state 可 curl | 零（订阅只读） | 中 |
| E. 工程链/部署/文档 | ★★ 横切所有 | ★★ 测试即验证 | 部署时 | 中 |

### 5.2 三条硬约束（决定怎么排）

1. **bridge.py 是单文件热点**：DogLink、FSM 接线、SbusStats、网页路由全在一个文件里，
   多 agent 同时改必然文本冲突。→ *并行前先做拆分（待办 2），拆完 D/E 类工作包才能真并行。*
2. **狗是单实例物理资源**：真机验证/部署互斥。→ *仿真层随便并行，真机层由主控统一排队*
   （一个 agent 占狗做验收时，其他只能跑离线测试；部署动作全局一次一个）。
3. **契约冻结**：/state 字段、WS 消息格式、RC 应答格式（`RC arm mode gear soc fresh`）、
   15010 包格式是模块间契约——已稳定并被两端（App/网页/桥）依赖，
   列入"冻结清单"，任何 agent 不得单方面改动；要改走主控评审 + 两端同步提交。

### 5.3 推荐协作模式（已被本项目验证的流程）

```
主控（有全局上下文, 唯一部署权/合并权/狗使用权）
 ├─ 派发: agent_i ← 独立 git 副本(或分支) + 冻结契约清单 + 安全边界 + 测试预算
 ├─ agent_i 离线开发: 只跑 run_test_shim 仿真 / 桩截图 / node --check
 ├─ 交付: diff + 证据(测试输出/截图) → 主控逐条核实(nitpick 纪律: 挑刺者不修)
 ├─ 合并门: 真身全量 15 套测试 → 主控部署上板(全局互斥) → 真机冒烟
 └─ 回归: 独立 agent 换角度验证 + 换视角猎杀(nitpick 第二轮范式)
```

- **已验证有效**：本日 FSM 调查、三轮 nitpick、公开仓快照全部走的"主控+独立 agent"模式，
  agent 抓出过主循环完全漏掉的确定性 bug（closed 漏改、_reject 炸线程即今日实例）。
- **不建议**：两个 agent 同时改 bridge.py（文本冲突+语义冲突双风险）；
  agent 自行部署/占狗（物理资源互斥 + 安全）；跳过合并门直接合入。

### 5.4 落地路线（把并行度从 1 提到 3）

1. 第一步（串行, 半天）：bridge.py 拆模块 —— `link.py`(DogLink) / `telemetry.py`(订阅+state 镜像)
   / `webserver.py`(路由) / `stats.py`(SbusStats)，bridge.py 保留装配+rc_loop；
   每拆一块跑全量测试，行为零变更。
2. 拆完后的常驻并行位：A(FSM) + B(网页) + C(App) 三个 agent 长期并行，
   D(遥测) 在 telemetry.py 独立后也可入列；真机验收进主控队列。
3. 每次 agent 交付必须附：改动文件清单 + 测试输出 + 「我改过什么」申报（nitpick 纪律）。
