# -*- coding: utf-8 -*-
"""setup4_newdog.py -- 新狗板(第四台)全套装机: 拆分架构代码 + 离线 wheels + venv +
fork 连狗库 + portaudio deb + pwm udev + 双 systemd 服务, 一条龙到服务起来。

前置(与 setup3 相同): 镜像已烙 netplan(eth0=123.50/144.10 别名), 板可 SSH(pi/pi)。
素材: wheels2.tar.gz / go2-bridge.service / go2-audio.service / uwc-fork-pkg.tar.gz /
      libjack + libportaudio2 deb (路径见下)。

与 setup3_dog3.py 的差别:
  1. 文件清单不再手写 -> import deploy_pc.collect_files() 自动发现(拆分后的
     stats/telemetry/link/webserver/core 全覆盖, 新增模块不再漏);
  2. site-packages 路径按板上 python3 实际版本探测, 不再写死 3.10;
  3. 不放 config_local.py(纯默认起步, 声道/差异听到后再调);
  4. 每步产物校验, 失败即停。

用法: python setup4_newdog.py
之后: BRIDGE_PW=pi BRIDGE_HOST=192.168.123.50 python deploy_pc.py 滚动部署。
"""
import os
import pathlib
import time

import paramiko

from deploy_pc import collect_files, git_version

HOST, USER, PW = "192.168.123.50", "pi", "pi"
LOCAL = pathlib.Path(__file__).parent
RDIR = "/home/pi/go2-bridge"
FORK_TAR = next(p for p in [
    pathlib.Path(os.environ.get("TEMP", r"C:\Users\Administrator\AppData\Local\Temp"))
    / "uwc-fork-pkg.tar.gz",
    pathlib.Path(r"D:\ZcodeRely\projects\go2\uwc-fork-pkg.tar.gz"),
] if p.exists())
DEBS = [r"D:\ZcodeRely\projects\walnutpi\debs\libjack-jackd2-0_1.9.20%7edfsg-1_arm64.deb",
        r"D:\ZcodeRely\projects\walnutpi\debs\libportaudio2_19.6.0-1.1_arm64.deb"]

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PW, timeout=10)


def run(cmd, t=600, quiet=False):
    # t=通道读超时: 静默命令(venv 创建无输出)也会被它掐, 长步骤要给足
    _, o, e = c.exec_command(cmd, timeout=t)
    out = (o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")).strip()
    if not quiet:
        print(out[:600])
    return out


print("== 0. 板环境探测 ==")
PYVER = run("python3 -c 'import sys; print(\"%d.%d\" % sys.version_info[:2])'").split()[-1]
print("板上 python3:", PYVER)
SP = "%s/.venv/lib/python%s/site-packages" % (RDIR, PYVER)

print("== 1. 建目录 + 传当前代码(collect_files 自动清单, %s) ==" % git_version(str(LOCAL)))
files = collect_files(str(LOCAL)) + ["requirements.txt"]   # 非依赖文件, collect 发现不了, 手动并入
print("清单 %d 个文件: %s" % (len(files), ", ".join(files)))
run("mkdir -p %s/static %s/core" % (RDIR, RDIR), quiet=True)
sftp = c.open_sftp()
for f in files:
    dest = "%s/%s" % (RDIR, f)
    sftp.put(str(LOCAL / f), dest)
sftp.close()
run("ls %s | wc -l && ls %s/static/" % (RDIR, RDIR))

def run_bg(cmd, log, max_s=3600):
    """长静默命令放板上后台跑, 轮询 .done 标记。
    教训(2026-11-02): venv/pip 这类几分钟无输出的命令, paramiko 通道读超时
    (exec_command timeout)一到就误报失败, 而板上进程还活着 -- 重跑还会和
    残留进程打架(.venv File exists 竞态)。改后台+标记, PC 断开也不影响。"""
    marker = log + ".done"
    run("rm -f %s %s" % (log, marker), quiet=True)
    run("( %s ; echo $? > %s ) > %s 2>&1 &" % (cmd, marker, log), quiet=True)
    t0 = time.time()
    while time.time() - t0 < max_s:
        time.sleep(20)
        st = run("cat %s 2>/dev/null || echo RUNNING" % marker, quiet=True).strip()
        if st != "RUNNING":
            print("  退出码 %s, 用时 %.0fs:" % (st, time.time() - t0))
            print(run("tail -3 %s" % log, quiet=True))
            return st
        print("  ... %ds" % (time.time() - t0), flush=True)
    raise SystemExit("!! %ds 仍未完成: %s" % (max_s, log))


print("== 2. 传离线 wheels (84MB, 视链路几分钟; 板上已有则跳过) ==")
have = run("ls %s/wheels 2>/dev/null | wc -l" % RDIR, quiet=True).split()
n_have = int(have[-1]) if have else 0
if n_have >= 30:
    print("板上已有 %d 个 wheel, 跳过上传" % n_have)
else:
    t0 = time.time()
    sftp = c.open_sftp()
    sftp.put(str(LOCAL / "wheels2.tar.gz"), "/tmp/wheels2.tar.gz")
    sftp.close()
    print("上传完成 %.0fs" % (time.time() - t0))
    run("cd /tmp && tar xzf wheels2.tar.gz -C %s && ls %s/wheels | wc -l" % (RDIR, RDIR))

print("== 3. venv + 离线装 ==")
out = run("cd %s && rm -rf .venv && python3 -m venv --without-pip .venv && echo VENV_OK" % RDIR, 900)
if "VENV_OK" not in out:
    raise SystemExit("!! venv 失败: " + out[-300:])
if run_bg("cd %s && pip3 install --no-index --find-links=./wheels "
          "--target=%s -r requirements.txt" % (RDIR, SP), "/tmp/setup4_pip.log") != "0":
    raise SystemExit("!! pip 安装失败(看 /tmp/setup4_pip.log)")
out = run("%s/.venv/bin/python -c 'import aiortc, serial, fastapi, uvicorn; "
          "print(\"IMPORTS_OK\")'" % RDIR, 60)
if "IMPORTS_OK" not in out:
    raise SystemExit("!! 依赖导入失败: " + out[-300:])

print("== 5. portaudio/jack deb (先于 fork 检查, 见上) ==")
sftp = c.open_sftp()
sftp.put(DEBS[0], "/tmp/libjack.deb")
sftp.put(DEBS[1], "/tmp/libportaudio2.deb")
sftp.close()
run("echo pi | sudo -S dpkg -i /tmp/libjack.deb /tmp/libportaudio2.deb 2>&1 | tail -2")
out = run("%s/.venv/bin/python -c 'import sounddevice; print(\"SD_OK\")'" % RDIR, 60)
if "SD_OK" not in out:
    raise SystemExit("!! sounddevice 仍不可用: " + out[-300:])

print("== 4. fork 连狗库覆盖 + curl_cffi ==")
# 2026-11-03 定案: deb(下一节)必须先于本节的导入检查 -- webrtc_driver 间接 import
# sounddevice(webrtc_audio), 全新板无 PortAudio 必炸; 顺序对调后检查放 deb 之后
sftp = c.open_sftp()
sftp.put(str(FORK_TAR), "/tmp/uwc-fork-pkg.tar.gz")
sftp.close()
run("cd %s && rm -rf unitree_webrtc_connect unitree_webrtc_connect-*.dist-info && "
    "tar xzf /tmp/uwc-fork-pkg.tar.gz -C %s" % (SP, SP))
if run_bg("pip3 install --no-index --find-links=%s/wheels --target=%s curl_cffi"
          % (RDIR, SP), "/tmp/setup4_curlcffi.log") != "0":
    raise SystemExit("!! curl_cffi 安装失败(看 /tmp/setup4_curlcffi.log)")
out = run("%s/.venv/bin/python -c \"from unitree_webrtc_connect.webrtc_driver import "
          "UnitreeWebRTCConnection, WebRTCConnectionMethod; "
          "from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD_MCF; "
          "print('FORK_LIBS_OK')\"" % RDIR, 60)
# 注意: 此处不 import sounddevice -- PortAudio deb 在下一步才装(全新板必挂, 2026-11-02 实踩)
if "FORK_LIBS_OK" not in out:
    raise SystemExit("!! fork 库导入失败: " + out[-300:])

print("== 6. pwm udev 规则 ==")
sftp = c.open_sftp()
with sftp.open("/tmp/98-pwm-pi.rules", "w") as f:
    f.write('SUBSYSTEM=="pwm", ACTION=="add|change", '
            'RUN+="/bin/sh -c \'chown -R pi:pi /sys%p; chmod -R g+w /sys%p\'"\n')
sftp.close()
run("echo pi | sudo -S cp /tmp/98-pwm-pi.rules /etc/udev/rules.d/ && "
    "sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=pwm")

print("== 7. 双 systemd 服务 ==")
sftp = c.open_sftp()
sftp.put(str(LOCAL / "go2-bridge.service"), "/tmp/go2-bridge.service")
sftp.put(str(LOCAL / "go2-audio.service"), "/tmp/go2-audio.service")
sftp.close()
run("echo pi | sudo -S cp /tmp/go2-bridge.service /tmp/go2-audio.service "
    "/etc/systemd/system/ && sudo systemctl daemon-reload && "
    "sudo systemctl enable go2-bridge go2-audio 2>&1 | tail -1")

print("== 8. 首启(狗在网, 应直接连上) ==")
run("echo pi | sudo -S systemctl restart go2-audio", quiet=True)  # 先音频后桥(P0-1)
run("echo pi | sudo -S systemctl restart go2-bridge", quiet=True)
time.sleep(10)
print("go2-audio:", run("systemctl is-active go2-audio", quiet=True))
print("go2-bridge:", run("systemctl is-active go2-bridge", quiet=True))
run("echo pi | sudo -S journalctl -u go2-bridge _PID=$(systemctl show -p MainPID "
    "go2-bridge --value) --no-pager 2>/dev/null | tail -6")
c.close()
print("==== 新狗板基础设施完成。下一步: BRIDGE_PW=pi BRIDGE_HOST=192.168.123.50 python deploy_pc.py")
