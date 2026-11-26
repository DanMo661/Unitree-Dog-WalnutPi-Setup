# -*- coding: utf-8 -*-
"""拉板子诊断日志到本地(只读): go2-bridge 日志/内核链路事件/网卡错误计数/资源"""
import os
import sys

import paramiko

HOST = os.environ.get("BRIDGE_HOST", "192.168.123.50")
PW = os.environ.get("BRIDGE_PW", "pi")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sbus_diag_tmp")

CMDS = [
    ("uptime_free.txt", "uptime; echo ---; free -h; echo ---; systemctl show go2-bridge -p ActiveEnterTimestamp -p NRestarts"),
    ("bridge_journal.txt", "sudo -S journalctl -u go2-bridge --no-pager --since '-26h' | tail -4000"),
    ("dmesg_eth.txt", "sudo -S dmesg -T | grep -iE 'eth0|link|phy|rtl|mdio' | tail -200"),
    ("dmesg_tail.txt", "sudo -S dmesg -T | tail -120"),
    ("eth_stats.txt", "ip -s link show eth0; echo ---; ip -s -s link show eth0"),
    ("kernel_journal.txt", "sudo -S journalctl -k --no-pager --since '-26h' | tail -500"),
]

def main():
    os.makedirs(OUT, exist_ok=True)
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username="pi", password=PW, timeout=8, look_for_keys=False, allow_agent=False)
    for fname, cmd in CMDS:
        stdin, stdout, stderr = cli.exec_command(cmd, timeout=60)
        if "sudo -S" in cmd:
            stdin.write(PW + "\n")
            stdin.flush()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace").strip()
        path = os.path.join(OUT, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"{fname}: {len(out.splitlines())} 行, {len(out)} 字节" + (f"  [stderr: {err[:120]}]" if err else ""))
    cli.close()
    print("[done] ->", OUT)

if __name__ == "__main__":
    sys.exit(main())
