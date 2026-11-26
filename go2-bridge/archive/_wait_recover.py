# -*- coding: utf-8 -*-
"""等板子网络恢复, 一恢复立即抓现场(uptime/journal/dmesg/transns), 纯只读"""
import os
import socket
import sys
import time

import paramiko

HOST = os.environ.get("BRIDGE_HOST", "10.243.173.31")
PW = os.environ.get("BRIDGE_PW", "pi")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sbus_diag_tmp", "dogwatch_recover.txt")
ROUNDS = int(os.environ.get("ROUNDS", "40"))   # 40 x 15s = 10 分钟

def tcp_ok(port=22, t=3):
    s = socket.socket(); s.settimeout(t)
    try:
        s.connect((HOST, port)); return True
    except Exception:
        return False
    finally:
        s.close()

def grab(cli):
    cmds = [
        "date '+%F %T'; uptime -s; uptime",
        "systemctl is-active go2-bridge wifi_watchdog NetworkManager",
        "ip -s -s link show eth0 | awk '/TX errors/{getline; print \"eth0 transns=\"$NF}'",
        "ip -4 addr show wlan0 | grep inet; iw dev wlan0 link | head -4",
        "sudo -S journalctl -u go2-bridge -u wifi_watchdog -u NetworkManager --no-pager --since '-40min'",
        "dmesg -T | grep -iE 'eth0|link is|phy|sprdwl|wlan' | tail -40",
    ]
    out = []
    for c in cmds:
        stdin, o, e = cli.exec_command(c, timeout=20)
        if "sudo -S" in c:
            stdin.write(PW + "\n"); stdin.flush()
        r = o.read().decode("utf-8", "replace").strip()
        out.append("$ " + c + "\n" + r)
    return "\n\n".join(out)

def main():
    with open(OUT, "a", encoding="utf-8") as f:
        for i in range(ROUNDS):
            ok = tcp_ok()
            print(f"[{time.strftime('%H:%M:%S')}] 探测 {i}: {'TCP22 通, 抓现场' if ok else '不通'}", flush=True)
            if ok:
                try:
                    cli = paramiko.SSHClient()
                    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    cli.connect(HOST, username="pi", password=PW, timeout=10,
                                look_for_keys=False, allow_agent=False)
                    data = grab(cli)
                    cli.close()
                    f.write(f"\n===== 恢复于 {time.strftime('%F %T')} =====\n" + data + "\n")
                    f.flush()
                    print(data[:2000], flush=True)
                    print("[现场已抓, 存到]", OUT, flush=True)
                    return 0
                except Exception as e:
                    print("恢复初期 SSH 仍不稳:", repr(e), flush=True)
            time.sleep(15)
    print("[10 分钟未恢复, 退出]", flush=True)
    return 1

if __name__ == "__main__":
    sys.exit(main())
