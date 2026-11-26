# -*- coding: utf-8 -*-
"""只读探测板子网络现状(经网线直连 123 网段), 不改任何东西.
用法: BRIDGE_HOST=192.168.123.50 BRIDGE_PW=pi python _net_recon.py
"""
import os
import sys

import paramiko

HOST = os.environ.get("BRIDGE_HOST", "192.168.123.50")
PW = os.environ.get("BRIDGE_PW", "pi")

CMDS = [
    ("身份/开机时长", "hostname && uptime"),
    ("网卡地址(含别名)", "ip -4 addr show"),
    ("路由", "ip route"),
    ("netplan 文件清单", "ls -l /etc/netplan/ 2>&1"),
    ("netplan 内容", "cat /etc/netplan/*.yaml 2>&1"),
    ("wpa_supplicant 运行实例", "pgrep -a wpa_supplicant"),
    ("wpa 运行时配置(密钥打码)", "sudo -S sed -E 's/(psk=).*/\\1***/' /run/wpa_supplicant/wlan0.conf 2>&1"),
    ("WiFi 关联状态", "iw dev wlan0 link 2>&1"),
    ("WiFi 省电状态", "iw dev wlan0 get power_save 2>&1"),
    ("WiFi 芯片/驱动", "lsmod | grep -iE 'sprd|uwe|aic|8800' ; ls /sys/class/net/wlan0/device/driver 2>&1"),
    ("networkd 视角", "networkctl status wlan0 --no-pager 2>&1 | head -30"),
    ("NetworkManager 在不在", "systemctl is-active NetworkManager 2>&1; which nmcli 2>&1"),
    ("go2-bridge 服务", "systemctl is-active go2-bridge 2>&1"),
    ("sudo 测试", "echo ok"),
]

def main():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username="pi", password=PW, timeout=8, look_for_keys=False, allow_agent=False)
    for title, cmd in CMDS:
        stdin, stdout, stderr = cli.exec_command(cmd, timeout=15)
        if "sudo -S" in cmd:
            stdin.write(PW + "\n")
            stdin.flush()
        out = stdout.read().decode("utf-8", "replace").strip()
        err = stderr.read().decode("utf-8", "replace").strip()
        print(f"\n===== {title} =====")
        if out:
            print(out)
        if err:
            print("[stderr]", err[:500])
    cli.close()
    print("\n[recon done]")

if __name__ == "__main__":
    sys.exit(main())
