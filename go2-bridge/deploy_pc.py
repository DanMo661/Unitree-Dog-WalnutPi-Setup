# -*- coding: utf-8 -*-
"""PC -> 板子一键烧录 go2-bridge(双版本感知): 上传 + md5 校验 + 板上语法门 + 重启 + 验活
用法: BRIDGE_PW=<板子密码> python deploy_pc.py [--src 源码目录] [--check-only]
  缺省 --src = 本脚本所在目录 —— 跑哪份源码里的 deploy_pc.py 就烧哪一版:
    go2-bridge/deploy_pc.py            -> 网络遥控版(含 netrc)
    go2-bridge-原版SBUS/deploy_pc.py   -> 纯 SBUS 原版(板上 netrc.py 会被删掉)
  --check-only 只做 md5 对比不上传不重启(只读, 可放心随时跑)
"""
import hashlib
import os
import pathlib
import sys
import time

import paramiko

HOST = os.environ.get("BRIDGE_HOST", "192.168.123.50")
USER = "pi"
REMOTE_DIR = "/home/pi/go2-bridge"
# 板上运行时文件全集(netrc.py 按源码目录里有没有而增删)
FILES = ["bridge.py", "config.py", "sbus.py", "servo.py", "pa.py", "pa_recv.py",
         "journal.py", "core/__init__.py", "core/fsm.py",
         "static/index.html", "static/sbus.html"]


def md5(p):
    return hashlib.md5(pathlib.Path(p).read_bytes()).hexdigest()


def main():
    args = sys.argv[1:]
    check_only = "--check-only" in args
    if "--src" in args:
        i = args.index("--src")
        if i + 1 >= len(args) or args[i + 1].startswith("--"):
            sys.exit("用法: python deploy_pc.py [--src 源码目录] [--check-only]")
        src = pathlib.Path(args[i + 1])
    else:
        src = pathlib.Path(__file__).parent
    if not (src / "bridge.py").exists():
        sys.exit("源码目录不对(缺 bridge.py): %s" % src)
    files = list(FILES) + (["netrc.py"] if (src / "netrc.py").exists() else [])

    pw = os.environ.get("BRIDGE_PW")
    if not pw:
        sys.exit("BRIDGE_PW 未设置")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    for i in range(4):
        try:
            c.connect(HOST, username=USER, password=pw, timeout=8)
            break
        except Exception as e:
            print("SSH 重试", i, repr(e))
            time.sleep(4)
    else:
        sys.exit("板子连不上")

    def run(cmd, timeout=60):
        _, o, e = c.exec_command(cmd, timeout=timeout)
        out = o.read().decode("utf-8", "replace")
        err = e.read().decode("utf-8", "replace")
        return o.channel.recv_exit_status(), out, err

    variant = "网络遥控版" if "netrc.py" in files else "纯SBUS原版"
    print("目标 %s@%s:%s  版本=%s  源=%s" % (USER, HOST, REMOTE_DIR, variant, src))

    # ---- md5 对比(先看差异, --check-only 到此为止) ----
    rc, out, _ = run("md5sum %s" % " ".join(REMOTE_DIR + "/" + f for f in files))
    remote = {}
    for line in out.strip().splitlines():
        p = line.split()
        if len(p) == 2:
            remote[p[1].replace(REMOTE_DIR + "/", "")] = p[0]
    diff = [f for f in files if remote.get(f) != md5(src / f)]
    for f in files:
        print(("  %-22s %s" % (f, "一致" if f not in diff else "不同")))
    if check_only:
        print("check-only 结束: %s" % ("全部一致" if not diff else "待更新 %d 个文件" % len(diff)))
        c.close()
        return

    # ---- 上传 + 校验 ----
    run("mkdir -p %s/core" % REMOTE_DIR)   # core/ 子目录: sftp.put 不会自动建目录
    sftp = c.open_sftp()
    for f in files:
        sftp.put(str(src / f), REMOTE_DIR + "/" + f)
    if "netrc.py" not in files:
        run("rm -f %s/netrc.py" % REMOTE_DIR)   # 回退原版: 板上残留的 netrc.py 一并清掉
        print("已删板上 netrc.py(回退原版)")
    sftp.close()
    # 板上 ext4 是 commit=600(10分钟才提交日志), 断电会回滚写入 —— sync 强制立即落盘
    run("sync")
    rc, out, _ = run("md5sum %s" % " ".join(REMOTE_DIR + "/" + f for f in files))
    bad = [l for l in out.strip().splitlines()
           if len(l.split()) == 2 and md5(src / l.split()[1].replace(REMOTE_DIR + "/", "")) != l.split()[0]]
    if bad:
        sys.exit("上传后 md5 不一致, 已中止(服务未动):\n" + "\n".join(bad))
    print("上传完成, md5 全部一致")

    # ---- 板上语法门: 编不过不许重启 ----
    # 纯内存 compile() 检查(不落 .pyc): py_compile 会写 __pycache__, 而 root 服务跑过
    # 一次后 pi 用户就写不进去, 每次部署都撞 Permission denied(10-17 实测两次)
    chk = ";".join("compile(open('%s',encoding='utf-8').read(),'%s','exec')" % (f, f)
                   for f in files if f.endswith(".py"))
    rc, out, err = run('cd %s && .venv/bin/python -c "%s"' % (REMOTE_DIR, chk))
    if rc != 0:
        sys.exit("板上语法检查失败, 已中止(服务未动):\n" + out + err)

    # ---- 重启 + 验活 (P0-1 音频拆分后双服务) ----
    # 新 bridge 不再在进程内启动音频: 板上必须有 go2-audio, 否则音频被静默丢掉。
    # 板上没有而本地有 unit -> 自动补装; 两边都没有 -> 中止部署。
    local_unit = src / "go2-audio.service"
    have_audio = run("systemctl cat go2-audio >/dev/null 2>&1")[0] == 0
    if not have_audio:
        if not local_unit.exists():
            sys.exit("板上无 go2-audio 且本地无 unit: 新 bridge 不再进程内启动音频, "
                     "继续部署会静默丢音频, 已中止")
        sftp = c.open_sftp()
        sftp.put(str(local_unit), "/tmp/go2-audio.service")
        sftp.close()
        run("echo %s | sudo -S cp /tmp/go2-audio.service /etc/systemd/system/ && "
            "sudo systemctl daemon-reload && sudo systemctl enable --now go2-audio" % pw)
        have_audio = run("systemctl cat go2-audio >/dev/null 2>&1")[0] == 0
        if not have_audio:
            sys.exit("go2-audio.service 补装失败, 已中止(音频会丢)")
        print("板上无 go2-audio, 已自动补装")
    run("echo %s | sudo -S systemctl restart go2-audio" % pw)   # 先音频后桥(OPTIMIZATION P0-1)
    run("echo %s | sudo -S systemctl restart go2-bridge" % pw)
    time.sleep(6)
    _, aout, _ = run("systemctl is-active go2-audio")
    print("go2-audio 状态:", aout.strip())
    if aout.strip() != "active":
        sys.exit("!! go2-audio 未激活, 音频不可用(遥控不受影响): 看 journalctl -u go2-audio")
    rc, out, _ = run("systemctl is-active go2-bridge")
    status = out.strip()
    print("服务状态:", status)
    if status != "active":
        sys.exit("服务未启动(is-active=%r), 部署失败! 先看板上 journalctl" % status)
    rc, out, _ = run("curl -s --max-time 3 http://127.0.0.1:8000/logs | tail -12")
    print("---- /logs 尾部 ----\n" + out)
    if not out.strip():
        sys.exit("服务 active 但 /logs 无内容(端口未就绪/进程异常), 部署判定失败!")
    c.close()
    if "Traceback" in out:
        sys.exit("启动日志有异常, 人工确认!")
    print("烧录完成 ✓  (App 侧: 对应版本的 PaSend APK 装上 G12 即可)")


if __name__ == "__main__":
    main()
