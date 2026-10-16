# go2-bridge 子项目约定

## 目的

Orange Pi Zero 2W 作遥控桥：云卓 G12 遥控器 SBUS → 板子解码 → WebRTC（MCF 指令）→ 宇树 Go2 Air，附浏览器遥控页（8000 端口）。
API 全部按官方库 legion1581/unitree_webrtc_connect（PyPI `unitree_webrtc_connect`）核实，不参考 `../go2rc/` 的参数。

## 目录与部署

- 本目录是源码唯一真身；板子上 `/home/pi/go2-bridge`（pi@192.168.123.50）只是部署副本。
- 板子网络（10-15 起）：eth0=123.50 + 144.10 别名（netplan 管，**不动**）；wlan0=NetworkManager 双档案（`board_wifi_setup.py` 部署）：家里「尧祐智能」静态 192.168.8.51 优先 / 手机热点「REDMI Turbo 4」DHCP（10.243.173.x），NM 层省电关 + wifi_watchdog.service。**热点上限 2，调试时注意名额**。
- `archive/` = 一次性诊断/探测脚本（不删除只归档）；`sbus_diag/` = 诊断日志正式归档（`sbus_diag_tmp/` 是被 gitignore 的落盘现场）。
- 推送一律走 `BRIDGE_PW=<板子密码> python deploy_pc.py`（上传+md5校验+板上py_compile语法门+重启+验活，编译不过不会重启服务）；`--check-only` 只读对比不动板子。
- `../go2rc/` 是他人发来的参考实现，只读留档：不维护、不部署、参数不得照抄（已发现 SpeedLevel/SwitchAvoidMode 参数名错误、Move 走等回复通道错误）。

## 双版本（2026-09-29 起，用户要求两手准备）

两套独立保存、都能一键烧录，用户说烧哪版就烧哪版：

- `OrangePi-Zero2W/go2-bridge/` = **网络遥控版**（App v35）：SBUS 线 + App「手动控制」双源；netrc(UDP:15010) 300ms 新鲜时优先于 SBUS 线，App 停发自动回退。板端代码基线 d882714（10-14 部署 dog2：删 pwm1、修趴下解锁弹起）。
- `OrangePi-Zero2W/go2-bridge-原版SBUS/` = **纯 SBUS 原版**（App v31）：无 netrc、无手动控制按钮，板端行为与 2026-09-29 上午部署的 v32 完全一致。

- 烧哪版：跑**那份源码里**的 `deploy_pc.py` 即烧那一版（回退原版时板上 netrc.py 会被自动删掉）。
- App 侧：`bash /d/ZcodeRely/android-build/build_apk.sh [App源码目录]`（缺省=网络遥控版）；两份成品 APK 已存各自版本目录（`PaSend-v34-网络遥控.apk（v34=解除音量30%限制，最新）` / `PaSend-v31-原版.apk`），装 G12 用 adb 或页面检查更新。APK 无 versionCode（默认1）同签名，`adb install -r` 两版直接互换，不存在降级拦截。**网络拓扑前提**：App 全部功能连板子的 `192.168.144.10` 别名（狗网段；板侧管理地址是 `192.168.123.50`，同板双 IP），App 内 IP 无设置入口属已知现状。
- 改动任一版本后必须重跑该版测试（见下）并重建对应 APK，保持"一说就烧"状态。

## 配置

- 所有可调参数只在 `config.py`。通道映射是假设值，**必须先跑 `sbus_monitor.py` 实测再改**。
- systemd 自启：`go2-bridge.service`（已 enable）。改配置后 `sudo systemctl restart go2-bridge`。

## 验证纪律

- 改完必跑（本地无 audioop 的 Python 用垫片）：`python run_test_shim.py test_sbus.py`、`test_link_sim.py`、`test_reconnect_sim.py`；网络遥控版另加 `test_netrc_sim.py`（网络优先/停发回退/实体接管/失控保护）和 `test_heartbeat_sim.py`（狗会话半死自动检测+拆会话）。两版本都必须绿。
- 板上运行日志：控制台「☰ 日志」面板（GET /logs，不用连网线）；systemd 日志用 `journalctl -u go2-bridge _PID=$(pgrep -f bridge.py) -o short-monotonic`（板子无 RTC，直接 -n 会被未来时间戳旧日志掩埋）。
- 连狗相关改动在板子在线时先用 `deploy_pc.py --check-only` 对比，真机行为未验证的步骤要明确告知用户。

## 安全红线

- 初期限速 VX≤0.5 m/s；失控保护现行口径（09-29 定）：运动中信号丢 → SBUS 线 0.25s 停走站定；App 网络源 ≈0.55s（FRESH_S 0.3 缓存 + 0.25 判停）；30s 真失联上锁**保持姿态不趴下**；仅 A 键主动上锁/网页退出才趴。每次改动代码后必须复测。
- Go2 固件锁 1.1.x（1.1.1~1.1.14 免密钥），**App 里严禁开固件自动升级**；≥1.1.15 需逐机取 AES key。
- 联调架空优先，远离楼梯、水域、人群；浏览器页关闭即自动上锁趴下。
