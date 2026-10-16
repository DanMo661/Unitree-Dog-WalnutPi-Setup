# -*- coding: utf-8 -*-
"""板#2 SSH 全套部署: go2-bridge 代码 + 离线 wheels + venv + systemd 服务 + 安装页
前置: setup2_serial_netplan.py 已配好 192.168.123.50; 板#1 断电
用法: python setup2_ssh.py
"""
import hashlib
import os
import pathlib
import tarfile
import time

import paramiko

HOST, USER, PW = "192.168.123.50", "pi", "pi"
LOCAL = pathlib.Path(__file__).parent
BRIDGE_FILES = ["bridge.py", "config.py", "sbus.py", "servo.py", "pa.py", "pa_recv.py",
                "sbus_diag.py", "requirements.txt"]
STATIC_FILES = ["static/index.html", "static/sbs.html" if False else "static/sbus.html"]
WHEELS_DIR = pathlib.Path(r"D:\ZcodeRely\walnutpi\wheels")
RDIR = "/home/pi/go2-bridge"

UNIT_BRIDGE = """[Unit]
Description=Go2 SBUS RC bridge (SBUS -> WebRTC MCF)
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=%s
ExecStart=%s/.venv/bin/python bridge.py
Environment=PYTHONUNBUFFERED=1
SupplementaryGroups=dialout
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
""" % (RDIR, RDIR)


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PW, timeout=10)

    def run(cmd, t=120):
        _, o, e = c.exec_command(cmd, timeout=t)
        return o.read().decode("utf-8", "replace").strip(), e.read().decode("utf-8", "replace").strip()

    print("== 1. 建目录 + 传代码 ==")
    run("mkdir -p %s/static" % RDIR)
    sftp = c.open_sftp()
    for f in BRIDGE_FILES:
        sftp.put(str(LOCAL / f), "%s/%s" % (RDIR, f))
    for f in ["index.html", "sbus.html"]:
        sftp.put(str(LOCAL / "static" / f), "%s/static/%s" % (RDIR, f))
    sftp.close()
    out, _ = run("ls -l %s/ | head -12" % RDIR)
    print(out)

    print("== 2. 传离线 wheels (85MB, 约2-4分钟) ==")
    tar_path = LOCAL / "wheels2.tar.gz"
    if not tar_path.exists():
        with tarfile.open(tar_path, "w:gz") as tf:
            for whl in WHEELS_DIR.glob("*.whl"):
                tf.add(whl, arcname="wheels/" + whl.name)
    print("本地包:", tar_path, "%dMB" % (tar_path.stat().st_size // 1048576))
    sftp = c.open_sftp()
    sftp.put(str(tar_path), "/tmp/wheels2.tar.gz")
    sftp.close()
    out, _ = run("cd /tmp && tar xzf wheels2.tar.gz -C %s && ls %s/wheels | wc -l" % (RDIR, RDIR))
    print("板上解出:", out, "个 wheel")

    print("== 3. venv + 离线安装 ==")
    # 镜像缺 python3.10-venv(ensurepip 拆包): --without-pip 建环境 + 系统 pip3 定向灌入
    out, err = run("cd %s && rm -rf .venv && python3 -m venv --without-pip .venv && echo VENV_OK" % RDIR, 180)
    print(out[-200:], err[-200:] if err else "")
    if "VENV_OK" not in out:
        print("!! venv 创建失败, 停在这, 通知我")
        c.close()
        return
    out, err = run("cd %s && pip3 install --no-index --find-links=./wheels "
                   "--target=./.venv/lib/python3.10/site-packages -r requirements.txt 2>&1 | tail -3" % RDIR, 600)
    print(out)
    out, _ = run("%s/.venv/bin/python -c 'import aiortc, serial, fastapi, uvicorn; "
                 "import unitree_webrtc_connect; print(\"IMPORTS_OK\")'" % RDIR, 60)
    print(out)

    print("== 4. systemd 服务 ==")
    sftp = c.open_sftp()
    with sftp.open("/tmp/go2-bridge.service", "w") as f:
        f.write(UNIT_BRIDGE)
    sftp.close()
    out, err = run("echo pi | sudo -S cp /tmp/go2-bridge.service /etc/systemd/system/ && "
                   "sudo systemctl daemon-reload && sudo systemctl enable go2-bridge 2>&1 | tail -1")
    print("enable:", out or err)

    print("== 5. 首启 + 日志 ==")
    run("echo pi | sudo -S systemctl restart go2-bridge")
    time.sleep(8)
    out, _ = run("systemctl is-active go2-bridge && "
                 "echo pi | sudo -S journalctl -u go2-bridge -n 15 --no-pager 2>/dev/null | tail -12")
    print(out)
    c.close()
    print("==== 完成。下一步: BRIDGE_USER=pi BRIDGE_PW=pi BRIDGE_HOST=192.168.123.50 python deploy_page.py ====")


if __name__ == "__main__":
    main()
