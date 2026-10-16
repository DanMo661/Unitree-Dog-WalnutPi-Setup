# -*- coding: utf-8 -*-
"""板子装机第二步: fork连狗库 + portaudio deb + pwm udev 规则
前置: setup3_dog3.py 已跑完(板在 192.168.123.50 可 SSH)
素材: fork 包 uwc-fork-pkg.tar.gz + 两个 deb(libjack/libportaudio, 目录用环境变量 DEBS_DIR 指定, 缺省脚本内置路径)
用法: python setup2_extras.py
"""
import os
import pathlib
import sys

import paramiko

HOST, USER = "192.168.123.50", "pi"
PW = os.environ.get("BRIDGE_PW", "pi")
SP = "/home/pi/go2-bridge/.venv/lib/python3.10/site-packages"
FORK_TAR_CANDIDATES = [
    pathlib.Path(os.environ.get("TEMP", ""))
    / "uwc-fork-pkg.tar.gz",
    pathlib.Path(r"D:\ZcodeRely\uwc-fork-pkg.tar.gz"),
]
import glob as _glob
_DEBS_DIR = os.environ.get("DEBS_DIR", r"D:\ZcodeRely\walnutpi\debs")
def _find_deb(pat):
    hits = sorted(_glob.glob(os.path.join(_DEBS_DIR, pat)))
    if not hits:
        raise SystemExit("在 %s 找不到 %s (环境变量 DEBS_DIR 可指定目录, deb 获取见 assets/MANIFEST.md)" % (_DEBS_DIR, pat))
    return hits[0]
DEBS = [_find_deb("libjack-jackd2-0_*.deb"), _find_deb("libportaudio2_*.deb")]
PWM_RULE = ('SUBSYSTEM=="pwm", ACTION=="add|change", '
            'RUN+="/bin/sh -c \'chown -R pi:pi /sys%p; chmod -R g+w /sys%p\'"\n')

fork_tar = next((p for p in FORK_TAR_CANDIDATES if p.exists()), None)
if not fork_tar:
    sys.exit("找不到 fork 包 uwc-fork-pkg.tar.gz (制作方法见 assets/MANIFEST.md 第五节)")

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PW, timeout=10)


def run(cmd, t=180):
    _, o, e = c.exec_command(cmd, timeout=t)
    return (o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")).strip()


print("== 1. fork 覆盖 PyPI 版 unitree_webrtc_connect + 补 curl_cffi ==")
sftp = c.open_sftp()
sftp.put(str(fork_tar), "/tmp/uwc-fork-pkg.tar.gz")
sftp.close()
print(run("cd %s && rm -rf unitree_webrtc_connect unitree_webrtc_connect-*.dist-info && "
          "tar xzf /tmp/uwc-fork-pkg.tar.gz -C %s 2>/dev/null | tail -1; ls %s/unitree_webrtc_connect | head -3"
          % (SP, SP, SP)))
print(run("cd /home/pi/go2-bridge && pip3 install --no-index --find-links=./wheels "
          "--target=%s curl_cffi 2>&1 | tail -1" % SP))
print(run("/home/pi/go2-bridge/.venv/bin/python -c \"from unitree_webrtc_connect.webrtc_driver import "
          "UnitreeWebRTCConnection, WebRTCConnectionMethod; "
          "from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD_MCF; "
          "import sounddevice; print('FORK_OK')\""))

print("== 2. portaudio deb (sounddevice 运行时依赖) ==")
sftp = c.open_sftp()
sftp.put(DEBS[0], "/tmp/libjack.deb")
sftp.put(DEBS[1], "/tmp/libportaudio2.deb")
sftp.close()
print(run("echo pi | sudo -S dpkg -i /tmp/libjack.deb /tmp/libportaudio2.deb 2>&1 | tail -2"))

print("== 3. pwm udev 规则 (舵机 sysfs 权限, 镜像自带规则只管 tty/gpio) ==")
sftp = c.open_sftp()
with sftp.open("/tmp/98-pwm-pi.rules", "w") as f:
    f.write(PWM_RULE)
sftp.close()
print(run("echo pi | sudo -S cp /tmp/98-pwm-pi.rules /etc/udev/rules.d/ && "
          "sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=pwm"))

print("== 4. 重启桥并验证 ==")
print(run("echo pi | sudo -S systemctl restart go2-bridge"))
import time
time.sleep(12)
print("状态:", run("systemctl is-active go2-bridge"))
print(run("echo pi | sudo -S journalctl _PID=$(systemctl show -p MainPID go2-bridge --value) "
          "--no-pager 2>/dev/null | grep -vE 'sudo|🕒' | tail -8"))
c.close()
print("==== extras 完成, 最后: BRIDGE_USER=pi BRIDGE_PW=pi BRIDGE_HOST=192.168.123.50 python deploy_page.py ====")
