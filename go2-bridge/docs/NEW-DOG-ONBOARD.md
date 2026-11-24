# 新狗上门流程（NEW-DOG-ONBOARD）

> 第四台狗（dog4）2026-11-02 全流程实跑后定稿。下一台狗照此走，卡在哪一步就查对应"坑"节。
> 分工是铁的：**人的事不要代劳，agent 的事不要上人。**

## 0. 分工总览

| 步骤 | 谁 | 做什么 |
|---|---|---|
| 1. 烧卡 | **人** | golden 镜像 raw 写 TF 卡（权威流程见 `../../provisioning/SD-BURN.md`） |
| 2. 装卡+硬件 | **人** | 卡插板、SBUS 接收机→板 40pin **pin10(RX2=ttyS2)**、网线进狗内网交换口、喇叭/继电器接线、上电 |
| 3. 推代码 | agent | 首板 `setup4_newdog.py` 一条龙；之后滚动 `deploy_pc.py` |
| 4. EEE | agent | **不用手动做**：deploy_pc 尾部守卫自动关+持久化（atexit，部署成败都跑） |
| 5. 板子无线调试 | agent | `board_wifi_setup.py` 配家里 WiFi 静态 IP（每板不同，见板位表） |
| 6. 遥控器接上+装 App | agent | USB 授权 → adb 装 APK → 配 WiFi → 无线 adb → App 指到板子 |
| 7. 遥控器通道标定 | **人** | agent 只需提醒一句"请调通道"，人自己会调，**agent 不要动** |
| 8. 验收 | 人+agent | 第 9 节清单逐项过 |

## 1. 前置素材（齐了再开工）

| 素材 | 位置 |
|---|---|
| golden 镜像 | `D:\ZcodeRely\projects\walnutpi\walnutpi-1b-golden.img` |
| 离线 wheels | `go2-bridge/wheels2.tar.gz`（84MB） |
| fork 连狗库 | `D:\ZcodeRely\projects\go2\uwc-fork-pkg.tar.gz` |
| 音频 deb×2 | `D:\ZcodeRely\projects\walnutpi\debs\`（libjack + libportaudio2） |
| systemd unit×2 | `go2-bridge/go2-bridge.service` + `go2-audio.service` |
| App APK | `go2-bridge/PaSend-v<N>-网络遥控.apk`（取最新版） |
| 板子密码 | pi / pi；狗 SSH root（见 credentials.md） |

镜像前置自检（烧完卡上电后）：`ping 192.168.123.50` 通（netplan 已烙 123.50+144.10 别名）、`ssh pi@…` 通。不通→串口 115200 8N1（pin8/10）看启动日志。

## 2. 推代码（agent）

```bash
# 首板(全新 golden, 无 go2-bridge 目录): 一条龙到服务起来
python setup4_newdog.py

# 滚动部署(此后一律这条, 顺带写 VERSION+冒烟+EEE 守卫):
BRIDGE_PW=pi BRIDGE_HOST=<狗内网IP>,<家里IP> python deploy_pc.py
```

- `BRIDGE_HOST` **必须显式**（默认列表不含新板，防裸跑烧错板）。
- 首板后必须跑一次 deploy_pc 把 `VERSION` 写上（setup4 不写它）。
- 部署尾部自动：**EEE 守卫**（狗侧关 EEE + `/etc/network/if-up.d/eee-off` 持久化；
  atexit 注册，部署失败也会跑）→ md5 全对 → 服务 active → `/state` 冒烟。

## 3. 板子无线调试（agent）

```bash
HOME_PSK=<家里WiFi密码> python board_wifi_setup.py --home-ssid "<家里WiFi名>" --home-ip 192.168.8.5X
```

- 静态 IP **每板不同**，占用见 CLAUDE.md 板位表（.51/.52/.53 已占，新板取下一个空位）。
- 装完板子双路可达：狗内网 IP + 家里 WiFi IP；wifi_watchdog 自动装。
- 热点兜底档案可选（`--hotspot-ssid/--hotspot-psk`，不给就跳过）。

## 4. 遥控器（agent）

1. **USB 连 PC**，遥控器上点「允许 USB 调试」（首次会弹）。
2. 装 App：`adb install -r PaSend-v<N>-网络遥控.apk`
3. 配 WiFi（二选一）：UI 驱动（`adb shell am start -a android.settings.WIFI_SETTINGS`
   + `input tap/text`，cmd wifi 被 SecurityException 挡时用）或人手点。
4. **无线调试**（拔线后继续可用）：`adb tcpip 5555` → `adb connect <遥控器IP>:5555`。
   注意：**遥控器重启后失效**，需重插 USB 再来一遍。
5. App 内「改板子地址」填板子 IP。App 默认探测 `192.168.144.10`（dock 有线路径）——
   遥控器 dock 在哪台狗上就连哪台；控家里 WiFi 段的板子就改成它的家里 IP。

## 5. 提醒人调通道（agent → 人）

说这一句就够：**"板子侧就绪，请调遥控器通道（A/B/C/D/三档/双摇杆），调完说一声。"**
不要自己跑 sbus_monitor 抓包对表——通道标定是人的活（换遥控器/接收机必标，
标定值口径见 `config.py` 通道映射节）。

## 6. EEE 的语义（为什么"自动"还会漏）

- EEE 关闭配置**每台狗一份**，落在狗自己的文件系统（`/etc/network/if-up.d/eee-off`）。
  同一台狗一次永久；**换狗/狗重置系统 = 出厂默认回 enabled，必须重来**。
- deploy_pc 守卫就是为这个设计的自动兜底。**2026-11-02 漏网教训**：守卫原先排在
  硬失败 exit 之后，新狗首部署 `/logs` 误判硬失败 → exit 跳过守卫 → EEE 漏网 →
  链路闪断。已改 **atexit 注册**（任何出口必跑）+ paramiko 直连隧道
  （旧 pty 二级跳在新狗上等不到密码提示会超时）。
- 人工复核一条命令（板子上）：
  `ssh root@<狗IP> "ethtool --show-eee eth0 | grep status"` → 要 disabled。

## 7. 验收清单（人上电后逐项过）

```bash
curl http://<板子家里IP>:8000/state     # ① ver=本次部署 ② link=up ③ sbus.ok=true/fps≈100
                                        # ④ rc_armed=false ⑤ gear/按键与遥控器实际位置一致
```

- [ ] 三服务 active：`systemctl is-active go2-bridge go2-audio go2-pa-page`
- [ ] `/logs` 有正常启动流（无 Traceback；黑匣子停机标记 clean）
- [ ] EEE disabled（第 6 节命令）
- [ ] App 显示「狗在线/数据干净」，按键状态回显与遥控器实物一致（= 通道已标对）
- [ ] 人实测四键：解锁→起立，上锁/趴下；B 键继电器吸合声
- [ ] 喊话有声（首次听声道对不对；不对改板上 `config_local.py` 的 `PA_STEREO`）
- [ ] 全过后打 `board-good-YYYYMMDD-N` tag

## 7b. 烧卡重装后的必做清单（2026-11-04 教训：漏一步静默残废）

重烧卡=回到出厂，以下按序全跑（缺一步缺一块）：

1. `python setup4_newdog.py`（代码+依赖+双服务）
2. **`HOME_PSK=<密码> python board_wifi_setup.py --home-ssid <WiFi名> --home-ip <板位IP>`**（11-04 漏跑 → WiFi 无档案静默失联）
3. DTB 启用 uart3（传感器口）：
   `sudo cp /boot/sun50i-h616-walnutpi-1b.dtb{,.bak-$(date +%m%d)}`
   `sudo fdtput -t s /boot/sun50i-h616-walnutpi-1b.dtb /soc/serial@5000c00 status okay`
   （pinctrl 已指 uart3-pi-pins=PI9/PI10，无需再改；重启后 `dmesg | grep ttyS3` 验证）
4. `BRIDGE_PW=pi BRIDGE_HOST=<IP> python deploy_pc.py`（写 VERSION+EEE 守卫+.eee_ok 标记）
5. 板上 `config_local.py`：`OVERRIDES = {"GAS_ENABLED": True, "GAS_PORT": "/dev/ttyS3"}`
6. 开机自检（启动 25s 后自动跑，/logs 看"开机自检"块）应 11 项全绿——**有红就是上面漏了**

## 8. 坑与教训（2026-11-02 实跑全集）

| 坑 | 症状 | 对策（已固化） |
|---|---|---|
| paramiko 通道读超时 | venv/pip 长静默命令被 PC 侧误判失败，板上进程还活着，重跑竞态炸 `.venv File exists` | setup4 长命令走**板上后台+标记轮询**（`run_bg`） |
| deb 顺序 | `import sounddevice` 在 PortAudio deb 之前 → 新板必炸 | setup4 已改序，fork 检查不 import sounddevice |
| collect_files 盲区 | requirements.txt 等非 import 文件不上板 | setup4 显式并入 |
| TF 卡 I/O 只读自保 | `mmc0: Card stuck being busy` → ext4 remount-ro，上传全挂 | 重启恢复；**再犯换卡**（该卡写入仅 ~3MB/s，本就慢） |
| 144.10 IP 打架 | dock 路径上狗本体与板子同抢 144.10 应答（MAC 实锤），App 时通时不通 | 遥控器优先走家里 WiFi 段 IP；避走 dock 段调试 |
| EEE pty 二级跳超时 | 守卫静默跳过没人看见 | 直连隧道 + atexit（第 6 节） |
| pkill 自匹配 | `pkill -f xxx` 匹配到执行它的命令行本身，自杀式 127 | `pkill -f 'xx[x]'` 字符类；杀/启拆两条命令 |
| PS 5.1 GBK 误读 | 无 BOM UTF-8 中文注释吞行 | 烧卡脚本纯 ASCII（见 ../../provisioning/SD-BURN.md） |
| 串口0(CPU-U0) 插外设致开机无网 | U-Boot autoboot 倒计时监听控制台, 外设上电杂波打断启动, 板子卡 U-Boot(电源灯亮/网络死) | 一次性 `python tools/uboot_bootdelay.py --port COM5` 置 bootdelay=0(USB-TTL 交叉接 CPU-U0); 应急=启动后再插传感器(桥的 gas 线程开着口等数据, 即插即出数) |
