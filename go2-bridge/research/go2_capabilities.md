# 宇树 Go2 能力清单（go2-bridge 视角）— 2026-10-21

> 权威口径：本桥实际使用的 SDK 2.0.0（官方库 `legion1581/unitree_webrtc_connect` v2.2）源码快照
> `research/sdk_snapshot/`（README.md:5），**api_id 与话题名全部以 `constants.py` 为准逐条对表**。
> 行号引用约定：`constants.py` / `webrtc_driver.py` / `msgs_pub_sub.py` / `webrtc_datachannel.py`
> 指 `research/sdk_snapshot/` 下同名文件；`bridge.py` / `config.py` 指项目根。
> 注：任务列出的 SOP.md 在项目根**不存在**（README.md:23 的文档分工里也只有 README/CLAUDE/
> ARCHITECTURE/OPTIMIZATION/TODO），现网行为以 README + ARCHITECTURE + config.py + bridge.py 为准。
> 标注口径：**在用**=本桥现网代码实际发送/订阅；**可发送/可订阅**=SDK 定义存在、本桥未用；
> **未确认**=参数格式或行为无法从代码/已有调研证实，禁止臆测。

---

## A. 网络通信全貌

### A.1 拓扑

```
G12 遥控器 ──2.4G──► G12 接收机 ──SBUS(100000bps 8E2, 100Hz, 反相)──► 板子 ttyS2(40pin pin10)
G12 App(手机)「手动控制」──UDP:15010──────────────────────────────►│ 网络遥控杆量(300ms 内新鲜则优先于 SBUS)
G12 App 喊话/动作/上传 ──UDP:15000/15001/15003───────────────────►│
浏览器遥控页 ──TCP:8000(HTTP+WS)────────────────────────────────►│  go2-bridge(单进程, 7 线程)
                                                                  └─ WebRTC ──► Go2 Air
                                                        信令 TCP:9991(con_notify) ──► 192.168.123.161
                                                        数据通道: rt/* 话题双向
```
（ARCHITECTURE.md:10-22、README.md:8-12）

狗内部另有一张 **DDS 域**（CycloneDDS 组播，话题 `rt/*`）：低层 `rt/lowcmd`/`rt/lowstate`、
`motion_switcher` 都走这条native路径，不经 WebRTC。板子 eth0 在狗内网（192.168.123.50），
与狗 L2 邻接，理论上可直接 DDS 收发（调研留底 §板端部署可行性）。

### A.2 端口/协议表

| 端口/链路 | 方向 | 协议 | 用途 | 出处 |
|---|---|---|---|---|
| **9991/TCP** | 板→狗(192.168.123.161) | WebRTC 信令 con_notify | 建会话/交换 SDP；看门狗每 3s TCP 探活 | config.py:11, ARCHITECTURE.md:60 |
| WebRTC 数据通道 | 板↔狗 | JSON(+二进制帧) over DTLS | 发 `rt/api/sport/request` 指令、订阅 `rt/*` 状态 | constants.py:75 |
| WebRTC 音视频轨 | 狗→板 | RTP | 摄像头视频/麦克风音频（VID/AUD 开关控制） | webrtc_datachannel.py:184-200 |
| **8000/TCP** | 浏览器→板 | HTTP+WebSocket | 遥控页 `/`、`/sbus`、`/logs`、`/ws`、`/state` | config.py:99, ARCHITECTURE.md:35 |
| **15000/UDP** | App→板 | 裸 PCM(S16LE) | 实时喊话音频流（板在 144 段 IP 收） | config.py:73, ARCHITECTURE.md:62 |
| **15001/UDP** | App→板 | UDP 行协议 | 喊话控制/音量/预设播放/`TRICK`动作/RC查询 | README.md:73-78, config.py:74 |
| **15003/UDP** | App→板 | 二进制分块+CRC | 预设音频上传（UPB/UPC/UPE 累计确认） | config.py:75 |
| **15010/UDP** | App→板 | UDP JSON | 网络遥控杆量（替代 SBUS 线） | config.py:12 |
| 15011/UDP | 板内 127.0.0.1 | UDP | TRICK 本地转发口（go2-audio 拆分后用，规划中） | config.py:78 |
| 15080/TCP | 手机→板 | HTTP | 喊话安装页+APK 上架（deploy_page.py 装） | README.md:43-44 |
| SBUS 串口 | 接收机→板 | 100000bps 8E2, 25B/帧, 16ch×11bit | 遥控信号线入 /dev/ttyS2 | config.py:21, ARCHITECTURE.md:68-69 |
| 云信令(Remote) | 板↔宇树云 | HTTPS+TURN | 库支持（token/公钥/TURN），**本桥未用** | webrtc_driver.py:63-71 |
| 狗内 DDS | 板↔狗(同网段) | CycloneDDS 组播 | 低层 rt/lowcmd(500Hz)/rt/lowstate、motion_switcher（低层路线用，未部署） | 调研留底 §通信机制 |

### A.3 网段

| 网段/地址 | 含义 | 出处 |
|---|---|---|
| 192.168.123.x | 狗内网（每狗独立）：狗本体 .161（webrtc_bridge），板子 .50（管理地址） | config.py:8, README.md:47, ARCHITECTURE.md:25 |
| 192.168.12.1 | 狗热点 LocalAP 模式下狗的地址（GO2-XXXXXX 热点） | webrtc_driver.py:87-89 |
| 192.168.8.51/.52 | 家 WiFi：dog2 板 .51 / dog3 板 .52 | README.md:47, ARCHITECTURE.md:91 |
| 192.168.144.10 | 板子在 App 侧（144 段）的别名，喊话音频收流地址 | ARCHITECTURE.md:25, config.py:73 |

### A.4 数据通道消息机制（发指令怎么走）

- 请求帧：`{"type":"req","topic":"rt/api/sport/request","data":{"header":{"identity":{"id":<int>,"api_id":<int>}},"parameter":"<json字符串>"}}`，
  可选 `policy.priority`（msgs_pub_sub.py:87-118）。
- 回包配对靠 uuid：`data.uuid` 或 `data.header.identity.id` 或 `data.req_uuid`（msgs_pub_sub.py:50-56）。
  回包判定：`data.header.status.code` 为 0/None/200 视为成功，其余记"被狗拒绝"（bridge.py:506-511）。
- **Move 特例**：高频流控走 noreply 免等回包——`policy:{priority:0,noreply:true}` + `publish_without_callback`
  （bridge.py:491-498），20Hz 上限（config.py:98）；队列里连续 Move 只发最新一条（bridge.py:483-488）。
- 订阅：`{"type":"subscribe","topic":...}`，取消 `unsubscribe`（msgs_pub_sub.py:120-140）。
- 二进制帧解析：头部 `<HH` 为 (2,0) 是雷达专用流；否则 `<H` json 长度 + json + 二进制体，
  含 "utlidar" 的话题走体素解码器（webrtc_datachannel.py:126-163）。
- 通道类型全集（DATA_CHANNEL_TYPE，constants.py:3-19）：validation / subscribe / unsubscribe / msg /
  req / res / vid / aud / err / heartbeat / rtc_inner_req / rtc_report / add_error / rm_error / errors。
- 会话保活：连接后 validation 握手→心跳启动（webrtc_datachannel.py:31-38）；本桥另叠加应用层心跳
  `GetSpeedLevel` 每 5s，连续 2 次无回包判"会话半死"拆链重连（config.py:15-17, bridge.py:384-414）。
- 连接方式三种：LocalSTA（同路由）/ LocalAP（狗热点 192.168.12.1）/ Remote（云）；本桥用 LocalSTA
  （config.py:7，webrtc_driver.py:21-24）。

### A.5 链路物理层已知坑（2026-10-21 定案）

狗侧 RTL8168 与只支持 10/100 的交换机做 EEE 协商后，会出现**每 5~15 秒 Link Down 恰好 3 秒再 Up**
（自协商重建耗时），表现为交换机狗口灯"亮几秒灭几秒"、ping 丢包、WebRTC 会话每分钟重连。
判定与修复：
```bash
ethtool --show-eee eth0          # 狗上：EEE status: enabled - active 即中招
ethtool --set-eee eth0 eee off   # 狗上：关闭后 carrier_changes 冻结、240 ping 0 丢失
```
设置只在运行时有效，狗重启后恢复 enabled（持久化需 systemd unit，属改狗系统配置，待批准）。
板载网卡不做 EEE，所以同交换机的板口不受影响。详见 `research/go2_network_diagnosis.md` §I。

---

## B. 数据通道话题表（RTC_TOPIC 全部 49 键，constants.py:62-120）

用途列为 SDK 源码注释+常量名的直译；凡本桥或调研未触过的细节标"未确认"。

| 键 | 话题 | 用途 | 本桥状态 |
|---|---|---|---|
| **SPORT_MOD** | `rt/api/sport/request` | 高层运动指令通道（C 节全部 api_id 走此话题） | **在用（唯一发送话题）** |
| **SPORT_MOD_STATE** | `rt/sportmodestate` | 高频姿态上报：mode/body_height/gait_type/velocity（部分固件不发） | **在用（订阅，bridge.py:424）** |
| **LF_SPORT_MOD_STATE** | `rt/lf/sportmodestate` | 姿态上报（另一路，两路都订谁到用谁） | **在用（订阅）** |
| LOW_STATE | `rt/lf/lowstate` | 低层全量状态（关节 q/dq/τ、IMU、BMS 嵌入式） | 可订阅（未用；DDS 版为 rt/lowstate） |
| LOW_CMD | `rt/lowcmd` | 低层关节指令（500Hz 位置/力矩流，CRC 校验） | **禁用**（PRO/AIR 固件疑锁，未探针，见 D） |
| MOTION_SWITCHER | `rt/api/motion_switcher/request` | 运动服务占用切换（CheckMode/ReleaseMode/SelectMode） | 可发送（低层接管门禁，未用；调研走 DDS 版） |
| WIRELESS_CONTROLLER | `rt/wirelesscontroller` | 遥控器按键/杆量状态 | 可订阅（未用） |
| OBSTACLES_AVOID | `rt/api/obstacles_avoid/request` | 避障开关/避障移动（api 见 C.6） | 可发送（未用） |
| VUI | `rt/api/vui/request` | 语音交互/灯效（VUI_COLOR 七色，constants.py:343-349） | 可发送（未用） |
| AUDIO_HUB_REQ | `rt/api/audiohub/request` | 板载音频库播放控制（api 见 C.7） | 可发送（未用；本桥喊话走自建 15000 链） |
| AUDIO_HUB_PLAY_STATE | `rt/audiohub/player/state` | 音频播放器状态 | 可订阅（未用） |
| BASH_REQ | `rt/api/bashrunner/request` | 远程执行 bash | **禁用**（攻击面，绝不接入） |
| FRONT_PHOTO_REQ | `rt/api/videohub/request` | 拍照请求 | 可发送（未用） |
| SELF_TEST | `rt/selftest` | 自检状态 | 可订阅（未用） |
| SERVICE_STATE | `rt/servicestate` | 服务状态 | 可订阅（未用） |
| MULTIPLE_STATE | `rt/multiplestate` | 用途未确认 | 未用 |
| GPT_FEEDBACK | `rt/gptflowfeedback` | GPT 流程反馈 | 可订阅（未用） |
| ASSISTANT_RECORDER | `rt/api/assistant_recorder/request` | 语音助手录音请求 | 可发送（未用） |
| ULIDAR_SWITCH | `rt/utlidar/switch` | 雷达开关 | 可发送（未用） |
| ULIDAR | `rt/utlidar/voxel_map` | 体素点云（二进制，需解码器） | 可订阅（未用） |
| ULIDAR_ARRAY | `rt/utlidar/voxel_map_compressed` | 压缩体素点云 | 可订阅（未用） |
| ULIDAR_STATE | `rt/utlidar/lidar_state` | 雷达状态 | 可订阅（未用） |
| ROBOTODOM | `rt/utlidar/robot_pose` | 里程计位姿 | 可订阅（未用） |
| UWB_REQ | `rt/api/uwbswitch/request` | UWB 开关请求 | 可发送（未用） |
| UWB_STATE | `rt/uwbstate` | UWB 状态 | 可订阅（未用） |
| GRID_MAP | `rt/mapping/grid_map` | 栅格地图 | 可订阅（未用） |
| SLAM_QT_COMMAND | `rt/qt_command` | SLAM 建图命令 | 可发送（未用） |
| SLAM_ADD_NODE / SLAM_ADD_EDGE | `rt/qt_add_node` / `rt/qt_add_edge` | 建图节点/边 | 可发送（未用） |
| SLAM_QT_NOTICE | `rt/qt_notice` | 建图通知 | 可订阅（未用） |
| SLAM_PC_TO_IMAGE_LOCAL | `rt/pctoimage_local` | 点云转图像 | 可订阅（未用） |
| SLAM_ODOMETRY | `rt/lio_sam_ros2/mapping/odometry` | LIO-SAM 里程计 | 可订阅（未用） |
| LIDAR_MAPPING_CMD | `rt/uslam/client_command` | uslam 建图命令 | 可发送（未用） |
| LIDAR_MAPPING_CLOUD_POINT | `rt/uslam/frontend/cloud_world_ds` | 建图点云 | 可订阅（未用） |
| LIDAR_MAPPING_ODOM | `rt/uslam/frontend/odom` | 建图里程计 | 可订阅（未用） |
| LIDAR_MAPPING_PCD_FILE | `rt/uslam/cloud_map` | 建图 PCD 文件 | 可订阅（未用） |
| LIDAR_MAPPING_SERVER_LOG | `rt/uslam/server_log` | 建图服务日志 | 可订阅（未用） |
| LIDAR_LOCALIZATION_ODOM | `rt/uslam/localization/odom` | 定位里程计 | 可订阅（未用） |
| LIDAR_LOCALIZATION_CLOUD_POINT | `rt/uslam/localization/cloud_world` | 定位点云 | 可订阅（未用） |
| LIDAR_NAVIGATION_GLOBAL_PATH | `rt/uslam/navigation/global_path` | 导航全局路径 | 可订阅（未用） |
| GAS_SENSOR / GAS_SENSOR_REQ | `rt/gas_sensor` / `rt/api/gas_sensor/request` | 气体传感器状态/请求 | 可订阅/可发送（未用） |
| PROGRAMMING_ACTUATOR_CMD | `rt/programming_actuator/command` | 图形化编程执行器命令 | 可发送（未用） |
| ARM_COMMAND / ARM_FEEDBACK | `rt/arm_Command` / `rt/arm_Feedback` | （人形）机械臂低层指令流/反馈 | 不适用（Go2 无臂） |
| ARM_REQUEST | `rt/api/arm/request` | （人形）上肢手势请求 | 不适用 |
| BMS_STATE / SECONDARY_IMU / MAIN_BOARD_STATE | `rt/lf/bmsstate` / `rt/lf/secondary_imu` / `rt/lf/mainboardstate` | （人形）拆分话题；Go2 把电池嵌 lowstate、单 IMU | 不适用（constants.py:97-104 注释） |

订阅机制注：雷达/点云大流量话题订阅前应发 `disable_traffic_saving on`（webrtc_datachannel.py:167-180）。

---

## C. 指令全表（SPORT_CMD_MCF，43 条，constants.py:178-222）

**通道**：全部走 `rt/api/sport/request`，帧格式见 A.4。api_id 逐条与 constants.py 对表。
**参数口径**：本桥对所有非 Move 指令一律发空 `parameter:"{}"` 且被接受（bridge.py:500-504）；
有据可查的参数格式单独注明，其余标"空参数/未确认"。
**前置口径**："本桥动作门"指 bridge.py 现网门禁（trick 需 mode=2 行走态+链路 up+无动作占用，
bridge.py:709）；"固件侧前置"未实测的一律标未确认。
**风险分级**：安全=日常可用｜谨慎=需人在场/首测架高｜危险=剧烈动作｜禁用=本桥明令不用
（翻滚/跳跃族 09-16 用户拍板移除，config.py:37-38）。

### C.1 姿态类（站/趴/坐/平衡/恢复）

| 指令 | api_id | 参数 | 前置 | 风险 | 说明/本桥用法 |
|---|---|---|---|---|---|
| BalanceStand | **1002** | 空 | 站立/起立完成后 | 安全 | 平衡站姿；起立流程收尾必补（bridge.py:650） |
| StandUp | **1004** | 空 | 趴姿 | 谨慎 | 起立；本桥起立后等 body_height 稳定再补 1002，防插进起立过程把狗弹起（config.py:120-128） |
| StandDown | **1005** | 空 | 站/走态 | 安全 | 趴下（上锁路径用）；坐姿→趴不发它（从坐姿猛塌，bridge.py:602-611） |
| Sit | **1009** | 空 | 站立态 | 安全 | 坐下（持续姿态）；本桥 TRICKS 在用，触发后本地 mode=3（bridge.py:717-722） |
| RiseSit | **1010** | 空 | 坐姿 | 未确认 | 从坐起立；本桥未用（坐→走走 StandUp 路径） |
| RecoveryStand | **1006** | 空 | 摔倒/侧翻态 | 谨慎 | 自主翻身恢复站立，动作剧烈；本桥未用 |
| Damp | **1001** | 空 | 任意（逃生口） | 谨慎 | 全关节泄力趴窝；高处泄力会砸地。本桥 damp() 仅手动保留，自动失控改走 StandDown（bridge.py:682-691） |
| BackStand | **2050** | 未确认 | 未确认 | 谨慎(未实测) | MCF 新增；语义未确认，不用 |

### C.2 移动类（速度/急停/限速）

| 指令 | api_id | 参数 | 前置 | 风险 | 说明/本桥用法 |
|---|---|---|---|---|---|
| Move | **1008** | `{"x":vx,"y":vy,"z":vyaw}`，x/y 单位 m/s、z 角速度 rad/s（bridge.py:731 + config.py:88-96 限速口径） | 解锁+mode=2+非起立中+无动作占用（bridge.py:728） | 谨慎(限速内安全) | 机体坐标系速度流；**走 noreply 免回包**（bridge.py:496-498），20Hz；本桥限速 VX/VY/VYAW=0.5/0.3/1.0，挡位上限见 config.GEAR_SPEEDS(0.9~2.5 m/s) |
| StopMove | **1003** | 空 | 任意 | 安全 | 幂等急停站定；失控保护第一步（bridge.py:737-742）、断链重连首条（bridge.py:764）、切模式前必发（bridge.py:612） |
| SpeedLevel | **1015** | `{"data":<int>}`（README.md:5 官方库口径；**本固件实测只收 -1/1**，bridge.py:694 注释） | 站立态 | 谨慎 | 速度档；本机三档挡杆实际改用步态指令实现 |
| SwitchJoystick | **1027** | 未确认 | 未确认 | 谨慎(未实测) | 摇杆模式切换；未用 |

### C.3 步态类

| 指令 | api_id | 参数 | 前置 | 风险 | 说明/本桥用法 |
|---|---|---|---|---|---|
| EconomicGait | **1063** | 空 | 站立/行走态 | 安全 | 经济步态；本桥挡1（bridge.py:697，实测可用） |
| FreeWalk | **2045** | 空 | 站立/行走态 | 安全 | 自由步态；本桥挡2（实测可用） |
| TrotRun | **1062** | 空 | 站立/行走态 | 谨慎 | 奔跑步态，速度快；本桥挡3（实测可用） |
| ContinuousGait | **1019** | 空 | 未确认 | 谨慎(未实测) | 未用 |
| StaticWalk | **1061** | 空 | 未确认 | 谨慎(未实测) | 未用 |
| ClassicWalk | **2049** | 空 | 未确认 | 谨慎(未实测) | 未用 |
| CrossStep | **2051** | 空 | 未确认 | 谨慎(未实测) | 未用 |
| FreeAvoid | **2048** | 未确认 | 未确认 | 谨慎(未实测) | 未用 |
| FreeBound | **2046** | 未确认 | 未确认 | 危险(未实测) | 跑跳步态族；不用 |
| LeadFollow | **2056** | 未确认 | 未确认 | 谨慎(未实测) | 跟随模式；不用 |

### C.4 身高/姿态调整

| 指令 | api_id | 参数 | 前置 | 风险 | 说明 |
|---|---|---|---|---|---|
| Euler | **1007** | 未确认 | 站立态 | 谨慎 | 机身 RPY 姿态调整；未用（调研留底路线 B 提及） |
| Pose | **1028** | 未确认（调研留底：BodyHeight+Roll/Pitch/Yaw，格式未实测） | 平衡站姿 | 谨慎 | 姿态+身高合成指令；未用 |
| （表外）BodyHeight | **1013**（经典表；MCF 表无此键） | `{"data":<相对值>}`，范围 -0.18~+0.03 m 相对默认站高 0.33m（蹲起方案文档口径） | 站立态 | 谨慎 | ⚠️ **1012 是 Trigger，不是身高**；经典表 `BodyHeight=1013`、MCF 表无此键，需手动补 `SPORT_CMD_MCF["BodyHeight"]=1013`；未上机 |

### C.5 动作与舞蹈

本桥动作门统一前置：**mode=2 + 链路 up + 无 pending + 冷却外**（bridge.py:709）；白名单在
config.TRICKS（8 项，含占用秒数=冷却锁，宁长勿短防两舞叠加 09-19 事故）。

| 指令 | api_id | 参数 | 风险 | 本桥状态 |
|---|---|---|---|---|
| Hello | **1016** | 空 | 安全 | **在用**（TRICKS, 4s） |
| Stretch | **1017** | 空 | 安全 | **在用**（5s） |
| Content | **1020** | 空 | 安全 | **在用**（撒娇, 4s） |
| Scrape | **1029** | 空 | 安全 | **在用**（刮挠, 5s） |
| Heart | **1036** | 空 | 安全 | **在用**（比心, 5s；注：经典表叫 FingerHeart，MCF 表叫 Heart，值同为 1036） |
| Dance1 | **1022** | 空 | 安全 | **在用**（摇摆舞, 24s 锁+配乐） |
| Dance2 | **1023** | 空 | 安全 | **在用**（39s 锁+配乐） |
| FrontFlip | **1030** | 未确认 | **禁用**（前空翻） | 白名单已剔除（config.py:37-38） |
| LeftFlip | **2041** | 未确认 | **禁用**（左侧翻） | 同上 |
| BackFlip | **2043** | 未确认 | **禁用**（后空翻） | 同上 |
| HandStand | **2044** | 未确认 | **禁用**（倒立） | 同上 |
| FrontJump | **1031** | 未确认 | **禁用**（前跳） | 同上 |
| FrontPounce | **1032** | 未确认 | **禁用**（前扑） | 同上 |
| FreeJump | **2047** | 未确认 | **禁用**（跳跃族） | 同上 |

### C.6 状态回读/配置

| 指令 | api_id | 参数 | 风险 | 本桥状态 |
|---|---|---|---|---|
| GetSpeedLevel | **1026** | `{}`，回包 `data.data`=挡位（bridge.py:400） | 安全 | **在用**：应用层心跳探活，5s 周期（bridge.py:384-398） |
| GetState | **1034** | 空 | 安全 | 未用 |
| SetAutoRecovery | **2054** | 未确认（疑 bool） | 谨慎 | 未用；关掉自恢复可能让狗摔倒后不动 |
| GetAutoRecovery | **2055** | 空 | 安全 | 未用 |
| SwitchAvoidMode | **2058** | `{"data":<n>}`（README.md:5 官方库口径） | 谨慎 | 未用 |

### C.7 低层（lowcmd / DDS，不走 SPORT_MOD）

- **通道**：DDS 直发 `rt/lowcmd`（500Hz）+ 订阅 `rt/lowstate`；WebRTC 侧亦有 `rt/lf/lowstate` 可订阅（constants.py:63,73）。
- **帧格式**：head=[0xFE,0xEF]、level_flag=0xFF、电机 mode=0x01(PMSM)、12 关节 q/kp/dq/kd/tau + CRC
  （CRC 错狗直接无视指令，失败方向安全）；位置式控制 kp=60/kd=5 起步（调研留底）。
- **关节序**：FR(髋0/大腿1/小腿2) → FL → RR → RL。官方示例关节角：站姿每腿 [0.0, 1.36, -2.65] rad，
  蹲姿 [0.0, 0.67, -1.3]；半蹲=两表 50% 插值 ≈ [0.0, 1.0, -2.0]（调研留底）。
- **接管门禁（必须按序）**：`CheckMode()` 确认运动服务占用 → 循环 `StandDown()+ReleaseMode()` 直到
  CheckMode 空（期间遥控器/App 全失效）→ lowcmd 才被电机执行 → 恢复 `SelectMode("ai")`。
  另有 WebRTC 话题 `rt/api/motion_switcher/request`（constants.py:119）可走同一门禁。
- **风险：危险**。PRO/AIR 出厂固件疑白名单锁 LowCmd（官方只开放 EDU）；本机老固件窗口期是否放行
  **未探针验证**（调研留底 §版本门禁）。断流时狗保持最后目标，须 Damp 兜底。

### C.8 相邻 API 域（本狗不适用或未用，备查）

- **避障** OBSTACLES_AVOID_API（constants.py:226-231，话题 `rt/api/obstacles_avoid/request`）：
  SWITCH_SET=1001 `{"enable":bool}`；SWITCH_GET=1002；MOVE=1003 `{"x","y","yaw","mode":0}`(noreply)；
  USE_REMOTE_COMMAND_FROM_API=1004。
- **音频库** AUDIO_API（constants.py:353-383，话题 `rt/api/audiohub/request`）：播放控制 1001-1010
  （列表/选曲播放/暂停/模式/重命名/删除）、上传 2001、内部语料 3001-3004/5001-5003、扩音器 4001-4003。
- **人形专属**（Go2 不适用，constants.py:233-341）：LOCO_API(7001-7109 FSM/速度)、R1_FSM 状态表、
  ARM_ACTION 手势(99-36)。列此防误用。

---

## D. 能做 / 不能做（本机：Go2 Air，固件 1.1.1 口径）

> 口径差说明：用户口径固件 1.1.1；README 固件红线写 1.1.1~1.1.14（免密钥窗口）；SDK 注释称 MCF
> api 空间 1.1.7 引入（constants.py:174-177）。本机 **1062/1063/2045 步态指令与 1001-1036 段指令
> 现网实测可用**（挡位功能与动作面板在用），即 MCF 段指令实际可用——精确固件号与 MCF 引入时间
> 的矛盾未单独考证，以实测为准。

### D.1 ✅ 能做（现网已验证在用）

1. WebRTC 全链路：9991 信令建会话、数据通道指令/订阅、应用层心跳探活+自动重连。
2. 高层姿态链：StandUp→(等狗自报 body_height 稳定)→BalanceStand→StandDown，含起立保护期与大动作互锁。
3. Move 速度流（noreply 20Hz，限速 0.5/0.3/1.0）+ StopMove 急停。
4. 三挡步态：EconomicGait/FreeWalk/TrotRun（SpeedLevel 本固件只收 -1/1，故挡位用步态实现）。
5. 8 项安全动作白名单（Hello/Stretch/Content/Scrape/Heart/Dance1/Dance2/Sit）+ 板端配乐联动。
6. 狗真实姿态订阅（sportmodestate 两路）：mode/body_height/gait_type/velocity，用于首连定档、
   起立完成判定、调试台显示。
7. 喊话/预设音频（自建 15000/15001/15003 链，不走狗固件音频 hub）。

### D.2 ⚪ SDK 有定义、本机理论上可用但未验证

- RecoveryStand 翻身、Euler/Pose 姿态调整、表外 BodyHeight(1013) 调身高（蹲起方案第一阶段，
  待首测 -0.03 小深度试探）。
- 其余 MCF 指令（C.2-C.6 标"未实测"项）：GetState、SwitchAvoidMode、避障 api、音频 hub api、
  VUI、拍照、UWB、雷达/建图订阅（大流量，需 disable_traffic_saving）。
- 视频轨（VID on/off）：库支持，本桥未拉流。

### D.3 🔒 被 PRO/AIR 固件/授权锁或明令禁用

1. **低层 LowCmd（rt/lowcmd 500Hz 关节流）**：官方口径只开放 EDU；社区实测 PRO/AIR 出厂固件电机
   不响应（疑固件白名单）。本机 1.1.1 老固件处于历史放行窗口期（调研留底：当时 ReleaseMode 未锁死
   有先例），**motion_switcher 探针未跑，放行与否未证实**。放行前按禁用处理。
2. **翻滚/跳跃族 7 指令**（C.5 标禁用）：用户 2026-09-16 拍板全部移出白名单（config.py:37-38）。
3. **BASH_REQ 远程 shell**：禁用（攻击面）。
4. **AES key 门槛**：Go2 固件 ≥1.1.15 起 Local 流程必须逐机 AES-128 key（32 hex，云接口
   `device/bind/list` 返回 dev.key，库内 `examples/fetch_aes_key.py` 获取，填 config.AES_KEY）
   （webrtc_driver.py:34-40、README.md:32-34）。本机 1.1.1 留空即可用；**App 里必须关闭"固件
   自动升级"锁住 1.1.x**，一旦 OTA 升过 1.1.15 本桥即断，需重新走 key 流程。
5. **MotionSwitcher ReleaseMode 副作用**：释放后狗失去自稳/遥控/App，直到 SelectMode("ai") 恢复；
   老固件窗口外（越狱前）不要碰。

### D.4 ⚠️ 已实测会触发的保护/危险行为（事故留底）

- **上装供电口保护性断电**（10-20）：狗 M8 扩展线对外 12V 给板/飞控供电，起立被第二条指令打断
  会弹起，电流冲击令 12V 口保护断电→板子掉电重启。软件止血=互锁+起立保护期+状态驱动起立
  （ARCHITECTURE.md:133-136、config.py:110-118）。
- **迟到的 BalanceStand 弹起**（10-17）：起立未完成时补 1002 会把趴着的狗瞬间弹起→模式切换即
  作废 pending/rising（bridge.py:597-600）。
- **两支舞叠加**（09-19）：动作冷却锁短于实际舞步会把两个动作叠到狗上→锁宁长勿短。
- **坐姿发 StandDown 猛塌**：坐→趴只记状态不发指令（bridge.py:602-611）。
- **App 单客户端限制**：测试本桥时宇树 App 必须退掉（README.md:54）。

---

## E. 安全约束（现网门禁全集）

### E.1 解锁/上锁（A 键，ch4，1002/1722 两档）

- 解锁上升沿去抖 1.0s（ARM_DEBOUNCE_S，config.py:115）；**上锁下降沿永不去抖**——任何时候都能
  上锁收权（bridge.py:574-575 注释）。
- 上锁路径必放行：互锁/保护期只拦起立方向，不拦趴下（防 A 键被吞、狗保持末速，10-20 复核抓出）。
- 串口重开后必须重新解锁（ARCHITECTURE.md:108）。

### E.2 起立（StandUp）三重门

1. **起立保护期** STAND_GUARD_S=5s：开机/进程启动后 5s 内拒绝任何会发 StandUp 的请求（供电口未稳）。
2. **大动作互锁** MODE_DWELL_S=2s：趴↔站两次执行至少间隔 2s，被拦请求由"请求标志+每帧重试"到点
   自动补发。
3. **状态驱动收尾**：StandUp 后进入 rising，摇杆一律不发；等狗自报 body_height 在 0.6s 窗口内波动
   <0.004m 且 >0.22m 才补 BalanceStand；无遥测回落固定 3s，最长 6s 兜底（config.py:120-128）。

### E.3 移动门

Move 仅 mode=2 且非 rising、非 pending、冷却外才发（bridge.py:728）；限速 VX/VY/VYAW=0.5/0.3/1.0
（config.py:96），挡位上限 0.9~2.5 m/s（config.py:88-92）；杆量归零发 StopMove。

### E.4 动作门

链路 up + mode=2 + 无 pending + 冷却外四个条件缺一不可（bridge.py:709）；TRICK 回包
`ERR LINKDOWN/NOTSTAND/BADTRICK/NOTREADY`（README.md:75）；Sit 记独立 mode=3，坐姿下 C 上位
不算数（防一坐下就被拉起，bridge.py:621-622）。

### E.5 失控/断链行为（分层）

| 情形 | 行为 | 出处 |
|---|---|---|
| SBUS 坏帧 | 站定等待恢复，不上锁 | ARCHITECTURE.md:105 |
| SBUS 断流 0.25s（FS_STOP_S） | StopMove 站定 + 继电器回安全位 | config.py:105 |
| 断流 30s（FS_DAMP_S） | 真失联：上锁+停走+**保持姿态不趴下**（仅 A 键主动上锁/网页退出才趴），须重新解锁 | config.py:106 |
| App 网络遥控源断 | ≈0.55s 停走站定 | README.md:57 |
| 狗链路断（载波/9991 探测） | 拆会话重连；**重连后只发 StopMove** 停走保持姿态 | bridge.py:756-764 |
| 狗会话半死（信令活/指令黑洞） | GetSpeedLevel 心跳 2 次无回包判死重连 | bridge.py:384-414 |
| 桥进程崩溃 | systemd Restart=always 5s 拉起 | ARCHITECTURE.md:109 |
| 开机首连 | **一条指令都不发**（狗开机自己站，打断即干扰），按狗自报高度定本地档 | bridge.py:756-759 |
| lowcmd 断流（若启用低层） | 狗保持最后目标——必须 Damp 兜底 + SelectMode("ai") 恢复 | 调研留底 §安全 |

### E.6 起立供电互锁的硬件背景

上装由狗对外 12V 供电，起立电流冲击可触发保护断电；硬件根治方案在 TODO（ARCHITECTURE.md:133-136）。
换板注意：dog2/dog3 继电器键位与音频声道策略不同，部署前必须确认（ARCHITECTURE.md:86-97）。

---

## F. 常用操作速查

| 操作 | 指令（api_id） | 前置 | 注意 |
|---|---|---|---|
| 连狗建链 | WebRTC 信令 9991 + validation + 心跳 | 板狗同网（LocalSTA/AP） | App 必须退掉（单客户端）；固件 ≥1.1.15 需 AES key |
| 探活 | GetSpeedLevel(1026)，5s 周期 | 链路 up | 任何回包（含拒收）都算活；2 次无回包重连 |
| 读狗姿态 | 订阅 rt/sportmodestate + rt/lf/sportmodestate | 链路 up | 超 1s 未更新不采信（DOG_STATE_STALE_S） |
| 解锁 | 本地放权（无狗指令） | 链路 up | A 键去抖 1s；上锁永远放行 |
| 上锁趴下 | StopMove(1003)→StandDown(1005) | 任意 | 走态先停再趴；坐姿直接趴会猛塌（坐→趴静默） |
| 起立 | StandUp(1004)→等高度稳定→BalanceStand(1002) | 解锁+过保护期+过互锁 | rising 期间摇杆全屏蔽；勿在起立中插任何指令 |
| 行走 | Move(1008) `{"x","y","z"}` noreply 20Hz | mode=2 且非 rising | 限速 0.5/0.3/1.0 起步；杆回中必发 StopMove |
| 急停 | StopMove(1003) | 任意 | 幂等，任何异常先发它 |
| 切挡/步态 | EconomicGait(1063)/FreeWalk(2045)/TrotRun(1062) | 行走态 | 本固件 SpeedLevel 只收 -1/1，挡位=步态 |
| 做动作 | TRICK 白名单 8 项（C.5） | mode=2+链路 up+冷却外 | 锁宁长勿短（防叠加）；配乐板端联动 |
| 调身高（蹲起） | BodyHeight(1013，经典表/MCF 表外) `{"data":-0.09~-0.03}`，需手动补 | 平衡站姿 | 首测 -0.03 起；1012=Trigger 别混 |
| 应急泄力 | Damp(1001) | 任意 | 仅手动；高处泄力会砸地 |
| 失控处置 | 0.25s 停走 / 30s 上锁保持姿态 | 自动 | 每次改代码后必测（README 调试第 4 步） |
| 翻滚/跳跃族 | FrontFlip(1030) 等 7 项 | — | **禁用**（用户拍板移除） |
| 低层关节流 | rt/lowcmd 500Hz（DDS） | ReleaseMode 成功 | 探针未跑按禁用；退出必 SelectMode("ai") |

---

## 附：狗侧错误码速查（app_error_messages，constants.py:26-60）

来源段：100/200=通信固件（DDS 超时/电池通信/风扇堵转）、300=电机（过流/过压/过热/编码器）、
400=雷达（转速/点云/串口）、500=UWB、600=运动控制（过热保护/低电保护）。狗侧错误经
`errors/add_error/rm_error` 类型消息推送（webrtc_datachannel.py:96-97），本桥未做展示（未用）。

## 附：经典 SPORT_CMD 与 MCF 表的差异（constants.py:122-172）

经典表多出 SwitchGait(1011)/Trigger(1012)/BodyHeight(1013)/FootRaiseHeight(1014)/Wallow(1021)/
GetBodyHeight(1024)/GetFootRaiseHeight(1025)/OnesidedStep(1303)/Bound(1304)/MoonWalk(1305)/
Handstand(1301)/CrossStep(1302)/StandOut(1039)/Standup(1050)/CrossWalk(1051) 等；MCF 表新增
1061-1063/2041-2058 段。**两表 1001-1036 段同值**。经典表 BodyHeight=1013（MCF 表无）、Trigger=1012；蹲起方案旧文误写 1012，已更正。本桥只发 MCF 表。

---
*依据文件：research/sdk_snapshot/{constants,webrtc_driver,webrtc_datachannel,msgs_pub_sub}.py、
research/go2_lowlevel_调研留底.md、research/蹲起动作方案.md、README.md、ARCHITECTURE.md、
config.py、bridge.py。未上板、未跑狗；所有"实测"字样均为仓库内既有记录，非本次验证。*
