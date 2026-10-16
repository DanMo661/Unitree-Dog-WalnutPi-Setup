# go2-bridge —— 板端遥控桥（SDK 转发层）

狗体内的主控板上跑的核心：把遥控器 SBUS 信号解码后经宇树 WebRTC SDK（MCF 指令）
发给狗主控，附浏览器调试台（8000 端口）。**安装与配置全流程见仓库根目录
[SOP.md](../SOP.md)，本文件只讲目录内容。**

```
G12 遥控器 ─2.4G→ GR01 接收机 ─SBUS(反相)→ /dev/ttyS2 ─→ bridge.py 解码/状态机/失控保护
                                              └→ WebRTC(MCF) → 狗主控 192.168.123.161:9991
```

## 目录速览

| 文件/目录 | 作用 |
|---|---|
| `bridge.py` | 主程序（三线程：狗链路/SBUS 串口/Web 服务），含心跳闭环与失控保护 |
| `config.py` | 全部可调参数（通道映射/限速/端口），**遥控器标定主要改这里**（另见 SOP §5） |
| `sbus.py` / `servo.py` / `netrc.py` / `pa*.py` | SBUS 解码 / PWM-GPIO 输出 / App 网络遥控 / 喊话音频 |
| `static/sbus.html` | 浏览器调试台（16 通道实时/帧率/链路状态） |
| `android-app/` | 遥控器 App 源码（GO2控制台：喊话台/动作面板/调试台） |
| `setup3_dog3.py` / `setup2_extras.py` / `deploy_page.py` / `deploy_pc.py` | 部署四件套（SOP 第 2 步） |
| `board_wifi_setup.py` | WiFi 双档案部署（SOP 第 3 步） |
| `research/` | Go2 低层控制调研留底（SDK2 关节级/固件解锁/MCF 指令表） |
| `legacy/` | 香橙派时代旧脚本归档，勿用 |
| `test_*.py` | 本地仿真测试（`python run_test_shim.py test_xxx.py`），改代码后必跑 |

## 安全要点

- 失控保护：SBUS 0.25s 停走站定；30s 真失联上锁保持姿态（不趴下）；网页端限速 0.5m/s，RC 按挡位（最高 2.5m/s，室内注意）
- 狗固件锁 1.1.x，**严禁开固件自动升级**
- 联调架空优先，远离楼梯/水域/人群
