# -*- coding: utf-8 -*-
"""dog3 板子全套装机: 当前代码 + 离线 wheels + venv + root 版 systemd 服务
前置: 镜像已烙 netplan(123.50/144.10), 板可 SSH; 素材 wheels2.tar.gz 在本目录
用法: python setup3_dog3.py
之后: python setup2_extras.py -> deploy_page.py
"""
import pathlib
import time

import paramiko

HOST, USER, PW = "192.168.123.50", "pi", "pi"
LOCAL = pathlib.Path(__file__).parent
BRIDGE_FILES = ["bridge.py", "config.py", "sbus.py", "servo.py", "pa.py",
                "pa_recv.py", "netrc.py", "journal.py", "sbus_diag.py", "requirements.txt"]
RDIR = "/home/pi/go2-bridge"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PW, timeout=10)


def run(cmd, t=120):
    _, o, e = c.exec_command(cmd, timeout=t)
    return (o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")).strip()


print("== 1. 建目录 + 传当前代码 ==")
run("mkdir -p %s/static" % RDIR)
sftp = c.open_sftp()
for f in BRIDGE_FILES:
    sftp.put(str(LOCAL / f), "%s/%s" % (RDIR, f))
for f in ["index.html", "sbus.html"]:
    sftp.put(str(LOCAL / "static" / f), "%s/static/%s" % (RDIR, f))
sftp.close()
print(run("ls %s/ %s/static/ | head -16" % (RDIR, RDIR)))

print("== 2. 传离线 wheels (88MB, 几分钟) ==")
sftp = c.open_sftp()
sftp.put(str(LOCAL / "wheels2.tar.gz"), "/tmp/wheels2.tar.gz")
sftp.close()
print(run("cd /tmp && tar xzf wheels2.tar.gz -C %s && ls %s/wheels | wc -l" % (RDIR, RDIR)), "个 wheel")

print("== 3. venv + 离线装 (镜像缺 ensurepip: --without-pip + 系统 pip3 定向灌) ==")
out = run("cd %s && rm -rf .venv && python3 -m venv --without-pip .venv && echo VENV_OK" % RDIR, 180)
if "VENV_OK" not in out:
    raise SystemExit("!! venv 失败: " + out[-300:])
print("VENV_OK")
print(run("cd %s && pip3 install --no-index --find-links=./wheels "
          "--target=./.venv/lib/python3.10/site-packages -r requirements.txt 2>&1 | tail -2" % RDIR, 600))
print(run("%s/.venv/bin/python -c 'import aiortc, serial, fastapi, uvicorn; "
         "import unitree_webrtc_connect; print(\"IMPORTS_OK\")'" % RDIR, 60))

print("== 4. root 版 systemd 服务 (GPIO sysfs 必须 root, 10-10 实测) ==")
sftp = c.open_sftp()
sftp.put(str(LOCAL / "go2-bridge.service"), "/tmp/go2-bridge.service")
sftp.close()
print(run("echo pi | sudo -S cp /tmp/go2-bridge.service /etc/systemd/system/ && "
          "sudo systemctl daemon-reload && sudo systemctl enable go2-bridge 2>&1 | tail -1"))

print("== 5. 首启(狗没开会处于等待循环, 正常) ==")
run("echo pi | sudo -S systemctl restart go2-bridge")
time.sleep(8)
print("状态:", run("systemctl is-active go2-bridge"))
print(run("echo pi | sudo -S journalctl -u go2-bridge -n 10 --no-pager 2>/dev/null | tail -8"))
c.close()
print("==== dog3 基础设施完成。下一步: python setup2_extras.py")
