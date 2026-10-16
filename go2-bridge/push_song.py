# -*- coding: utf-8 -*-
"""把本地 pcm(16k单声道) 推到板子预设音频库 /root/pa-files/<名>.pcm
用法: BRIDGE_HOST=<板子IP> BRIDGE_PW=<板子密码> python push_song.py <本地.pcm> <板上的名字>
流程: SFTP(pi)先落 /tmp, 再 sudo 挪进 /root/pa-files, 最后 UDP LIST 验证在库
"""
import os
import socket
import sys

import paramiko


def main():
    src, name = sys.argv[1], sys.argv[2]
    if " " in name:
        raise SystemExit("名字不能带空格(PLAY 行协议按词切分)")
    host = os.environ.get("BRIDGE_HOST", "192.168.123.50")
    pw = os.environ.get("BRIDGE_PW", "pi")

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username="pi", password=pw, timeout=10)
    sftp = cli.open_sftp()
    sftp.put(src, "/tmp/_song.pcm")
    sftp.close()
    _, out, err = cli.exec_command(
        "sudo mv /tmp/_song.pcm '/root/pa-files/%s.pcm' && ls -la /root/pa-files/ | tail -5" % name)
    print(out.read().decode("utf-8", "replace"))
    e = err.read().decode("utf-8", "replace").strip()
    if e:
        print("stderr:", e)
    cli.close()

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(4)
    s.sendto(b"LIST\n", (host, 15001))
    try:
        data, _ = s.recvfrom(65536)
    except socket.timeout:
        print("LIST 无响应(15001)")
        return 1
    txt = data.decode("utf-8", "replace")
    print("LIST:", txt[:400])
    return 0 if name in txt else 1


if __name__ == "__main__":
    sys.exit(main())
