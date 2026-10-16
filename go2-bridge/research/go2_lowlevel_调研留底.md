# Go2 底层控制（LowCmd）调研留底 — 2026-10-14

目标动作：**站 → 蹲到一半 → 站起 → 蹲到一半 → 站起**。用户拍板走路子二（关节级底层控制）。
本档为可行性调研结论，未动任何代码/板子。

---

## 路线 A：SDK2 低层控制（关节级，正路）

### 组件与依赖
- **unitree_sdk2py**（github.com/unitreerobotics/unitree_sdk2py，官方，822★，2026-09 仍在更新）
  纯 Python，依赖 `cyclonedds==0.10.2` + numpy（opencv 只有相机示例要）。
- cyclonedds aarch64 wheel 待板上验证；不行就源码编译（纯 C，cmake 无第三方依赖，
  FAQ 官方路径：clone releases/0.10.x → cmake install → CYCLONEDDS_HOME pip -e）。
- 参考示例已存本地：`<本地参考代码目录>`
  （go2_stand_example.py / unitree_legged_const.py / motion_switcher_example.py）

### 通信机制（从官方示例实证）
- 话题：发布 `rt/lowcmd`（500Hz，RecurrentThread 0.002s）＋订阅 `rt/lowstate`（关节 q/dq/τ、IMU、电池）
- LowCmd 帧要点：head=[0xFE,0xEF]、level_flag=0xFF、电机 mode=0x01(PMSM)、
  12 关节 q/kp/dq/kd/tau + **CRC 校验**（unitree_sdk2py.utils.crc.CRC）
- 位置式控制：kp=60 kd=5 dq=0 tau=0，目标 q 平滑插值（示例做法：从 lowstate 实测起始角
  按百分比插值到目标，每段 500~1000 个周期）
- **LegID 顺序**：FR(髋0/大腿1/小腿2) FL RR RL，各 [hip, thigh, calf]

### 接管流程（关键门禁）
```
1. MotionSwitcherClient.CheckMode() -> 有运动服务占用?
2. 循环: SportClient.StandDown() + MotionSwitcherClient.ReleaseMode()
   直到 CheckMode 返回空   ← 运动服务下线, 遥控器/App 全失效
3. 之后 lowcmd 才被电机执行; 恢复 = SelectMode("ai")
```

### 本动作现成的关节角表（官方示例即站→蹲→保持→起立）
- 站姿 targetPos_1：每腿 [0.0, 1.36, -2.65]（hip, thigh, calf，rad）
- 蹲姿 targetPos_2：每腿 [0.0, 0.67, -1.3]
- **"蹲到一半" = 两表 50% 线性插值**（thigh≈1.0, calf≈-2.0），
  序列：站→插值到半蹲→回站→再半蹲→回站，全程 SmoothStep 缓动
- PosStopF=2.146e9 / VelStopF=16000.0（占位停机值）

### ⚠️ 版本门禁（PRO 大概率被锁，但咱固件老，值得实测）
- 官方口径：低层开放给 **EDU**；PRO/AIR 出厂固件 LowCmd 电机不响应（社区实测，疑固件白名单）
- 越狱路线（若被锁）：root 狗内置算力模块后用社区 **UniversalControlKit**；
  Reddit 两帖：Go2 Air/Pro 逆向工程 + jailbreak and low-level control（见调研来源）
- **咱的狗固件 1.1.1~1.1.14（AES 空串时代）= 2024 老固件窗口期，PRO 的 ReleaseMode
  当时未被锁死是有先例的 → 板上跑 motion_switcher_example 探针零成本验证，先测再定**

### 板端部署可行性
- 板已在狗 LAN（eth0=123.50），DDS 组播天然可达；`ChannelFactoryInitialize(0, "eth0")`
- 跑在板上而非 PC/WSL（DDS 要 L2 邻接，PC 不在狗网段）
- 需在板装 pip 依赖（cyclonedds 等）→ 动手前须用户点头（装依赖红线）

### 安全
- CRC 错 → 狗直接无视指令（失败方向安全）
- ReleaseMode 后狗失去自稳/遥控：先趴/架高测试；kp/kd 从小往大；恢复靠 SelectMode("ai")
- lowcmd 断流（进程崩）→ 狗保持最后目标，需 Damp 兜底命令

---

## 路线 B（意外收获）：MCF `Pose`(1028) 调身高 — 零门禁立即可做

现有 WebRTC SPORT_MOD 指令表里有 **Pose=1028**（还有 Euler=1007）：站立/BalanceStand 态下
可调 **BodyHeight + Roll/Pitch/Yaw**。"蹲到一半站起来"本质就是身高参数下-上-下-上，
**不需要低层控制、不需要 ReleaseMode、PRO 也能用**（和 Dance1 同一条通道）。
- 做法：StandUp+BalanceStand 后循环发 Pose(BodyHeight=低/正常)，各保持 1~2s
- 待实测：参数格式（BodyHeight 归一化还是米制、范围）——发一条看回包/狗反应即知
- 局限：只有身高/姿态自由度，不算真·自定义动作；但作为第一个"自己编的动作"够用且当天可上

## 建议动手顺序
1. **先试路线 B**：板上 bridge 发 Pose(1028) 调身高（探参数→封装成"蹲起"动作→进动作面板）
2. 路线 A 探针：板上装 sdk2py 跑 CheckMode/ReleaseMode，确认老固件 PRO 是否放行低层
3. 放行 → 用官方示例关节表实现真·关节级蹲起（半蹲=50% 插值）；被锁 → 评估越狱值不值

## 越狱工具链（2026-10-14 补充，用户点名找的"破解社区"）

- **a-bissell/UnLeash-Lite**（28★，2026-08 更新）：Go2 WebRTC 越狱主武器，固件 **1.1.7~1.1.15**
  - fw ≤1.1.13：`ssh` 模式全自动 → `ssh root@192.168.123.161`（密码 unleash），无需 AES
  - fw 1.1.14：`init-ssh` + 遥控器 **L1+Y 按两次** 触发
  - fw ≥1.1.15：要 AES key（Cloud Intel 用宇树账号拉，或 root 后读 /unitree/etc/key/aes_key.bin）
  - 10 种 payload：ssh/ssh-persist/custom/reverse-shell/bypass-*(绕 1.1.14 加的 seccomp+黑名单)
  - ⚠️ 自己的警告：init-ssh 后必须立刻删 sitecustomize.py 否则狗上所有 Python 服务每次开机崩
  - 原理=同作者 CVE 库 a-bissell/go2-webrtc-signaling（信令无认证→数据通道全开，CVSS 9.3）
- **mobitouch-robotics/UniversalControlKit**（34★）：Air/Pro/Edu 统一底层控制前端（Reddit 越狱帖配套）
- **abizovnuralem/go2_ros2_sdk**（1059★）：非官方 ROS2 SDK，PRO 走 WebRTC、EDU 走 DDS
- 社区大本营：Reddit r/unitree（越狱/底层控制两帖是入口）
- **对咱的意义**：咱狗 1.1.1~1.1.14 在覆盖范围内；拿到 root 后路线 A 的 PRO 门禁可解
  （直接跑 DDS/打补丁放开 LowCmd），蹲起 Pro 随意编。动手前须用户明确点头（变砖/保修/OTA 风险）。

## 调研来源
- 官方 SDK2py: github.com/unitreerobotics/unitree_sdk2py（go2_stand_example / motion_switcher）
- 宇树文档: support.unitree.com developer（Quick_start / Basic_motion_control / Basic_services）
- PRO 锁与越狱: reddit.com/r/unitree 「Go2 Air/Pro Reverse Engineering」「jailbreak and lowlevel control」
- UniversalControlKit: reddit.com/r/unitree 开源控制工具帖
