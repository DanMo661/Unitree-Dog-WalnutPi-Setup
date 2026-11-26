# -*- coding: utf-8 -*-
"""狗链路闪断实时监控(纯只读): journal增量 + dmesg链路事件 + eth0 carrier翻转计数
每 30s 一轮, 共 ROUNDS 轮(默认18轮=9分钟), 结果 append 到本地 dogwatch_live.txt
"""
import os
import sys
import time

import paramiko

HOST = os.environ.get("BRIDGE_HOST", "10.243.173.31")
PW = os.environ.get("BRIDGE_PW", "pi")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sbus_diag_tmp", "dogwatch_live.txt")
ROUNDS = int(os.environ.get("ROUNDS", "18"))

def snap(cli):
    """一轮采集: 返回 (板钟, transns, journal增量, dmesg尾部)"""
    _, o, _ = cli.exec_command("date '+%F %T'", timeout=8)
    ts = o.read().decode().strip()
    _, o, _ = cli.exec_command(
        "ip -s -s link show eth0 | awk '/TX errors/{getline; print $NF}'", timeout=8)
    transns = o.read().decode().strip()
    stdin, o, _ = cli.exec_command(
        "sudo -S journalctl -u go2-bridge --since '-32s' --no-pager "
        "| grep -vE '^--|^$' | tail -40", timeout=12)
    stdin.write(PW + "\n"); stdin.flush()
    jr = o.read().decode("utf-8", "replace").strip()
    _, o, _ = cli.exec_command(
        "dmesg -T 2>/dev/null | grep -iE 'eth0|link is|phy' | tail -4", timeout=8)
    dm = o.read().decode("utf-8", "replace").strip()
    return ts, transns, jr, dm

def main():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username="pi", password=PW, timeout=10,
                look_for_keys=False, allow_agent=False)
    prev_jr = ""
    with open(OUT, "a", encoding="utf-8") as f:
        for i in range(ROUNDS):
            try:
                ts, transns, jr, dm = snap(cli)
            except Exception as e:
                line = f"[轮{i}] SSH 采集异常: {e!r}"
                print(line, flush=True); f.write(line + "\n"); f.flush()
                try:
                    cli.close()
                except Exception:
                    pass
                cli = paramiko.SSHClient()
                cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                cli.connect(HOST, username="pi", password=PW, timeout=10,
                            look_for_keys=False, allow_agent=False)
                continue
            line = f"[{ts}] transns={transns}"
            print(line, flush=True)
            f.write(line + "\n")
            if jr != prev_jr:
                for l in jr.splitlines():
                    print("  J|", l, flush=True)
                    f.write("  J| " + l + "\n")
                prev_jr = jr
            if dm:
                for l in dm.splitlines():
                    print("  D|", l, flush=True)
                    f.write("  D| " + l + "\n")
            f.flush()
            if i < ROUNDS - 1:
                time.sleep(30)
    cli.close()
    print("[监控结束]", flush=True)

if __name__ == "__main__":
    sys.exit(main())
