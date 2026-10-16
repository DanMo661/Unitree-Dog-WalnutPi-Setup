# -*- coding: utf-8 -*-
"""板子 wlan0 从 netplan 挪交给 NetworkManager, 建 主WiFi/手机热点 双档案 + 看门狗.

无线调试设计: 主WiFi(公司/家里/现场) = 静态IP + 高优先级(10), 在场必连;
             手机热点 = DHCP + 低优先级(5) 兜底, 主WiFi不在场时自动顶上, 随处可调试.

用法(PC 上跑, 网线直连或同网段可达):
  HOME_PSK=<主WiFi密码> python board_wifi_setup.py \
      --home-ssid "<现场主WiFi名>" \
      --hotspot-ssid "<手机热点名>" --hotspot-psk "<热点密码>" \
      --home-ip 192.168.X.5X    # ★ 主网下板子静态IP, 每块板不同(dog2=.51 dog3=.52 ...)

eth0(123.50 + 144.10 桥)不动。
"""
import argparse
import ipaddress
import os
import sys
import time

import paramiko

HOME_SSID = os.environ.get("HOME_SSID", "")   # 现场主WiFi名, 经 --home-ssid 或环境变量传入
HOME_PSK = os.environ.get("HOME_PSK", "")     # 主WiFi密码, 经环境变量传入不进仓库

WATCHDOG_SH = """#!/bin/bash
# wifi_watchdog: wlan0 关联但网关丢包>=50% 连续2轮 -> 重连(NM autoconnect 接管)
# 2026-10-15 加, 治 unisoc UWE5622 僵死。只在已坏时动作, 健康时零开销。
bad=0
while true; do
    sleep 60
    if ! iw dev wlan0 link 2>/dev/null | grep -q 'SSID:'; then
        continue   # 未关联, NM autoconnect 负责重试
    fi
    gw=$(ip route show default | awk '/dev wlan0/{print $3; exit}')
    [ -z "$gw" ] && continue
    loss=$(ping -I wlan0 -c 4 -W 2 -q "$gw" 2>/dev/null | grep -oE '[0-9]+% packet loss' | grep -oE '^[0-9]+')
    [ -z "$loss" ] && loss=100
    if [ "$loss" -ge 50 ]; then
        bad=$((bad+1))
        logger -t wifi_watchdog "gw $gw loss ${loss}% (round $bad)"
        if [ "$bad" -ge 2 ]; then
            logger -t wifi_watchdog "bouncing wlan0"
            nmcli device disconnect wlan0 >/dev/null 2>&1
            sleep 3
            nmcli device connect wlan0 >/dev/null 2>&1
            bad=0
        fi
    else
        bad=0
    fi
done
"""

WATCHDOG_UNIT = """[Unit]
Description=wifi watchdog for unisoc wlan0
After=NetworkManager.service

[Service]
Type=simple
ExecStart=/usr/local/bin/wifi_watchdog.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


def run(cli, pw, cmd, timeout=60, check=False):
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    if "sudo -S" in cmd:
        stdin.write(pw + "\n")
        stdin.flush()
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace").strip()
    err = stderr.read().decode("utf-8", "replace").strip()
    if check and rc != 0:
        raise RuntimeError(f"rc={rc} cmd={cmd[:120]}\nout={out[:500]}\nerr={err[:500]}")
    return rc, out, err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hotspot-ssid", required=True)
    ap.add_argument("--hotspot-psk", required=True)
    ap.add_argument("--home-ssid", default=HOME_SSID,
                    help="现场主WiFi名(公司/家里), 也可用环境变量 HOME_SSID")
    ap.add_argument("--home-ip", required=True,
                    help="主WiFi网段下板子的静态 IP, 每块板不同(第1块=.51 第2块=.52 依次递增)")
    ap.add_argument("--home-gw", default=None,
                    help="主网网关(默认按 x.x.x.1 推导; 网关是 .254/.0.1 的现场显式传)")
    ap.add_argument("--host", default=os.environ.get("BRIDGE_HOST", "192.168.123.50"))
    ap.add_argument("--pw", default=os.environ.get("BRIDGE_PW", "pi"))
    a = ap.parse_args()
    HOME_SSID = a.home_ssid
    for opt in ("home_ip",) + (("home_gw",) if a.home_gw else ()):
        ipaddress.IPv4Address(getattr(a, opt))   # 非法 IP 立即报错, 不等到 nmcli
    GW = a.home_gw or (a.home_ip.rsplit('.', 1)[0] + '.1')  # 网关/DNS: 显式指定, 缺省按 x.x.x.1 推导
    if not HOME_SSID or not HOME_PSK:
        sys.exit("缺 --home-ssid / HOME_PSK(主WiFi密码): 主WiFi档案没法建")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(a.host, username="pi", password=a.pw, timeout=8, look_for_keys=False, allow_agent=False)
    P = lambda c, **kw: run(cli, a.pw, c, **kw)  # noqa: E731

    print("== 0. 改动前快照 ==")
    P("nmcli -t device status; echo ---; ls /etc/netplan/")

    print("== 1. 检查 NM 没把 wifi 排除在管理外 ==")
    rc, out, _ = P("grep -r unmanaged /etc/NetworkManager/ 2>/dev/null; true")
    if "unmanaged-devices" in out and "wlan" in out:
        print("!! NM 配置排除了 wifi, 先处理:\n", out)
        sys.exit(1)
    print("    干净")

    print("== 2. 备份并撤下 netplan 的 wifi 配置 ==")
    P("if [ -f /etc/netplan/40-wifi.yaml ]; then "
      "sudo -S cp -n /etc/netplan/40-wifi.yaml /etc/netplan/40-wifi.yaml.bak_1015 && "
      "sudo -S mv /etc/netplan/40-wifi.yaml /root/40-wifi.yaml.retired && "
      "sudo -S netplan apply; else echo '(已撤下, 跳过)'; fi", check=True)
    time.sleep(3)
    P("sudo -S nmcli device set wlan0 managed yes 2>/dev/null; sudo -S nmcli device set wlan0 autoconnect yes 2>/dev/null; true")

    print("== 3. 建两个连接档案 ==")
    P("sudo -S nmcli connection delete home 2>/dev/null; sudo -S nmcli connection delete hotspot 2>/dev/null; true")
    P(f"sudo -S nmcli connection add type wifi ifname wlan0 con-name home ssid '{HOME_SSID}' "
      f"wifi-sec.key-mgmt wpa-psk wifi-sec.psk '{HOME_PSK}' "
      f"ipv4.method manual ipv4.addresses {a.home_ip}/24 "
      f"ipv4.gateway {GW} ipv4.dns '{GW} 223.5.5.5' "
      f"ipv6.method auto connection.autoconnect yes connection.autoconnect-priority 10 "
      f"connection.autoconnect-retries 0 "
      f"wifi.powersave 2", check=True)
    P(f"sudo -S nmcli connection add type wifi ifname wlan0 con-name hotspot ssid '{a.hotspot_ssid}' "
      f"wifi-sec.key-mgmt wpa-psk wifi-sec.psk '{a.hotspot_psk}' "
      f"ipv4.method auto ipv6.method auto "
      f"connection.autoconnect yes connection.autoconnect-priority 5 "
      f"connection.autoconnect-retries 0 "
      f"wifi.powersave 2", check=True)

    print("== 4. 装看门狗 ==")
    sftp = cli.open_sftp()
    with sftp.file("/tmp/wifi_watchdog.sh", "w") as f:
        f.write(WATCHDOG_SH)
    with sftp.file("/tmp/wifi_watchdog.service", "w") as f:
        f.write(WATCHDOG_UNIT)
    sftp.close()
    P("sudo -S mv /tmp/wifi_watchdog.sh /usr/local/bin/wifi_watchdog.sh && "
      "sudo -S chmod 755 /usr/local/bin/wifi_watchdog.sh && "
      "sudo -S mv /tmp/wifi_watchdog.service /etc/systemd/system/wifi_watchdog.service && "
      "sudo -S systemctl daemon-reload && "
      "sudo -S systemctl enable --now wifi_watchdog", check=True)

    print("== 5. 验证 ==")
    print(P("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device")[1])
    print(P("nmcli -f NAME,AUTOCONNECT,AUTOCONNECT-PRIORITY,TYPE connection show")[1])
    print(P("systemctl is-active wifi_watchdog; iw dev wlan0 get power_save")[1])
    print(P("nmcli device wifi list 2>/dev/null | head -8")[1])
    cli.close()
    print("""
[完成] 下一步人工验证:
  1. 手机开热点 -> 板子自动连(60s 内) -> 手机热点客户端列表查板子 IP
  2. PC WLAN 也连热点 -> ssh pi@<板子IP>
  3. 主WiFi(公司/家里)在场 -> 板子自动回静态 --home-ip(优先级更高)
""")


if __name__ == "__main__":
    sys.exit(main())
