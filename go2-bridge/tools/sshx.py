# -*- coding: utf-8 -*-
"""sshx.py -- 板子 ad-hoc 远程命令助手(只读探查用, 不做改动)。

用法: BRIDGE_PW=<pw> python tools/sshx.py <host> "<command>"
连接参数与 deploy_pc.py 同源: 用户 pi, 密码 BRIDGE_PW 环境变量(默认 pi)。
多命令用 && / ; 拼在一条里; 退出码=远端命令退出码。
"""
import os
import sys

import paramiko


def main():
    if len(sys.argv) < 3:
        print("usage: sshx.py <host> <command>")
        return 2
    host, cmd = sys.argv[1], sys.argv[2]
    pw = os.environ.get("BRIDGE_PW", "pi")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="pi", password=pw, timeout=8, banner_timeout=8)
    _, o, e = c.exec_command(cmd, timeout=120)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    rc = o.channel.recv_exit_status()
    c.close()
    if out:
        print(out.rstrip())
    if err.strip():
        print("[stderr] " + err.strip(), file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
