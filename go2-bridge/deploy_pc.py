# -*- coding: utf-8 -*-
"""PC -> 板子一键烧录 go2-bridge(双版本感知): 上传 + md5 校验 + 板上语法门 + 重启 + 验活 + /state 冒烟
用法: BRIDGE_PW=<板子密码> python deploy_pc.py [--src 源码目录] [--check-only]
  缺省 --src = 本脚本所在目录 —— 跑哪份源码里的 deploy_pc.py 就烧哪一版:
    go2-bridge/deploy_pc.py            -> 网络遥控版(含 netrc)
    go2-bridge-原版SBUS/deploy_pc.py   -> 纯 SBUS 原版(板上 netrc.py 会被删掉)
    go2-bridge/deploy_pc.py --src go2-bridge/_deploy_dog2
                                       -> dog2 板级变体(代码与主树同步,
                                          config.py 只差 ROBOT_IP/ETH_IFACE/
                                          SERVO1_CH/CH_RELAY 四处板级差异,
                                          并自带 config_local.py(PA_STEREO=False),
                                          不依赖板上残留配置)
  --check-only 只做 md5 对比不上传不重启(只读, 可放心随时跑)
  BRIDGE_HOST 可逗号分隔多地址(默认 192.168.123.50,192.168.8.52), 逐个尝试, 每地址 2 次(P1-1)
  每次部署写板端 VERSION=git short HEAD, /state 返回 ver; 重启后 /state 冒烟:
  服务非 active / /state 不可达 / /state.ver != 本次 VERSION = 硬失败,
  link/sbus/audio 异常只打 WARN 不失败(P1-1)
"""
import hashlib
import json
import os
import pathlib
import re as _re
import shlex
import subprocess
import sys
import tempfile
import time

import paramiko

# P1-1: 有线口(狗内网)与家里 WiFi 会轮流闪断, 单地址部署常被打断 -> 逐个尝试
HOSTS = [h.strip() for h in os.environ.get(
    "BRIDGE_HOST", "192.168.123.50,192.168.8.52").split(",") if h.strip()]
USER = "pi"
REMOTE_DIR = "/home/pi/go2-bridge"
# 板上运行时文件全集(netrc.py / config_local.py 按源码目录里有没有而增删)
FILES = ["bridge.py", "config.py", "sbus.py", "servo.py", "pa.py", "pa_recv.py",
         "journal.py", "core/__init__.py", "core/fsm.py",
         "static/index.html", "static/sbus.html"]


def md5(p):
    return hashlib.md5(pathlib.Path(p).read_bytes()).hexdigest()


def git_version(src):
    """P1-1 版本号: 默认 git short HEAD; 目录自带 VERSION(发行快照, 由 make_release.py 生成)优先。
    注意快照目录在同一父仓库内时 git 会返回父仓库 HEAD, 所以本地 VERSION 必须先看。"""
    src = pathlib.Path(src)
    try:
        v = (src / "VERSION").read_text(encoding="utf-8").strip()
        if v:
            return v
    except OSError:
        pass
    try:
        r = subprocess.run(["git", "-C", str(src), "rev-parse", "--short", "HEAD"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        v = r.stdout.decode("utf-8", "replace").strip()
        if r.returncode == 0 and v:
            return v
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def git_dirty(src):
    """工作区有未提交改动(仅提示): VERSION 只记 HEAD, 板上代码可能领先该提交。"""
    try:
        r = subprocess.run(["git", "-C", str(src), "diff-index", "--quiet", "HEAD", "--"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return r.returncode == 1
    except (OSError, subprocess.SubprocessError):
        return False


def dog_eee_guard(c, robot_ip, dog_pw):
    """部署尾部顺带保障: 狗侧 EEE 必须关(2026-10-21 dog2 定案: RTL8168 与百兆对端协商
    EEE 会链路瞬断; 10-24 dog3 漏网教训: 只装一台, 换狗/重置就回enabled)。
    经板子 invoke_shell 二级跳 SSH 到狗(板上零安装), 开着就关+补 if-up.d 持久化。
    best-effort: 狗不可达/无凭据/任何异常只提示不失败, 不影响部署门禁。"""
    import base64 as _b64
    import re as _re
    # 持久化钩子内容(ASCII 注释, 避免交互回显乱码): 开机自动关 EEE, 删文件即回退
    hook = ("#!/bin/sh\n"
            "# EEE off: RTL8168 EEE with 100M partner causes link flaps (2026-10-21)\n"
            "ethtool --set-eee eth0 eee off 2>/dev/null || true\n")
    hook_b64 = _b64.b64encode(hook.encode()).decode()
    probe = ("if ethtool --show-eee eth0 2>/dev/null | grep -q 'EEE status: enabled'; "
             "then ethtool --set-eee eth0 eee off; echo EEE_FIX_APPLIED; fi; "
             "[ -x /etc/network/if-up.d/eee-off ] || { echo %s | base64 -d "
             "> /etc/network/if-up.d/eee-off && chmod +x /etc/network/if-up.d/eee-off "
             "&& echo EEE_PERSIST_INSTALLED; }; "
             "ethtool --show-eee eth0 2>/dev/null | grep -q 'EEE status: enabled' "
             "&& echo EEE_STILL_ENABLED || echo EEE_DISABLED_OK" % hook_b64)
    try:
        sh = c.invoke_shell(width=200)
        time.sleep(1.0)
        if sh.recv_ready():
            sh.recv(65536)                      # 丢登录 banner
        sh.send("stty -echo\n")                 # 关回显: 标记词判断不被命令文本污染
        time.sleep(0.5)
        if sh.recv_ready():
            sh.recv(65536)
        sh.send("ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 root@%s\n" % robot_ip)
        buf, dl = "", time.time() + 12
        while time.time() < dl:                 # 等密码提示(狗可达)或超时(没开机)
            time.sleep(0.4)
            if sh.recv_ready():
                buf += sh.recv(65536).decode("utf-8", "replace")
            if _re.search(r"[Pp]assword", buf):
                break
        if not _re.search(r"[Pp]assword", buf):
            print("EEE 自检: 狗 %s SSH 不可达(没开机/不在网), 跳过 —— 换狗/重置后记得确认 EEE 已关"
                  % robot_ip)
            sh.close()
            return
        sh.send(dog_pw + "\n")
        time.sleep(2.5)
        if sh.recv_ready():
            sh.recv(65536)
        sh.send("stty -echo\n")                 # 狗端 pty 同样关回显
        time.sleep(0.5)
        if sh.recv_ready():
            sh.recv(65536)
        sh.send(probe + " ; echo EEE_PROBE_END\n")
        out, dl = "", time.time() + 25
        while time.time() < dl:
            time.sleep(0.4)
            if sh.recv_ready():
                out += sh.recv(65536).decode("utf-8", "replace")
            if "EEE_PROBE_END" in out:
                break
        sh.send("exit\nexit\n")
        time.sleep(0.3)
        sh.close()
        if "EEE_FIX_APPLIED" in out:
            print("EEE 自检: 狗侧 EEE 原来开着, 已关闭")
        if "EEE_PERSIST_INSTALLED" in out:
            print("EEE 自检: 已补装 /etc/network/if-up.d/eee-off 持久化(删文件即回退)")
        if "EEE_STILL_ENABLED" in out:
            print("!! WARN EEE 自检: 关闭后仍是 enabled, 人工检查狗侧 ethtool")
        elif "EEE_DISABLED_OK" in out:
            print("EEE 自检: 狗侧 EEE=disabled, 持久化在位 ✓")
        else:
            print("EEE 自检: 输出异常(狗上缺 ethtool?), 人工确认. 输出片段: %r" % out[-200:])
    except Exception as e:
        print("EEE 自检异常(不影响部署): %r" % e)


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
    if (src / "config_local.py").exists():
        files.append("config_local.py")   # 板级差异(不进 git): 有就一起烧; 本地没有则不动板上原文件

    # ---- 变体代码落后硬门(2026-10-24 dog2 教训): --src 指向变体目录时, 代码文件必须与
    # 本脚本所在目录(真身)一致。否则 md5 门只比"板上 vs 变体"(两边同旧, 全一致),
    # 旧代码会顶着新 VERSION 上板, /state 报新版本号但跑的是旧逻辑 ----
    home = pathlib.Path(__file__).parent.resolve()
    if src.resolve() != home:
        stale = [f for f in files
                 if f.endswith(".py") and f not in ("config.py", "config_local.py")
                 and (home / f).exists() and (src / f).exists()
                 and md5(home / f) != md5(src / f)]
        if stale:
            sys.exit("变体目录代码落后于真身: %s\n先从真身刷新变体再部署"
                     "(旧代码顶着新 VERSION 上板的事故 10-24 实际发生过)" % stale)

    # P1-1 版本化: VERSION=git short HEAD, 和运行文件一起上传, bridge /state 的 ver 读它。
    # 写临时文件而非源码目录(不脏工作区), 后面走同一套 md5 对比/上传/校验。
    version = git_version(src)
    ver_path = pathlib.Path(tempfile.mkdtemp(prefix="go2-ver-")) / "VERSION"
    ver_path.write_bytes((version + "\n").encode("utf-8"))   # 按字节写: Windows 文本模式会把 \n 变 \r\n
    files.append("VERSION")

    def lpath(f):
        return ver_path if f == "VERSION" else src / f

    pw = os.environ.get("BRIDGE_PW")
    if not pw:
        sys.exit("BRIDGE_PW 未设置")
    qpw = shlex.quote(pw)   # sudo -S 走 shell: 密码含 $/;/| 等字符时不被注入/断裂
    # P1-1 双地址: 每个地址 2 次(每次新建 client, 失败后旧连接不可复用)
    host, c = None, None
    for h in HOSTS:
        for i in range(2):
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                cli.connect(h, username=USER, password=pw, timeout=8,
                            banner_timeout=8, auth_timeout=8)
                host, c = h, cli
                break
            except Exception as e:
                cli.close()
                print("SSH %s 第 %d 次失败: %r" % (h, i + 1, e))
                time.sleep(2)
        if c is not None:
            break
    if c is None:
        sys.exit("板子连不上(已试: %s)" % ", ".join(HOSTS))

    def run(cmd, timeout=60):
        _, o, e = c.exec_command(cmd, timeout=timeout)
        out = o.read().decode("utf-8", "replace")
        err = e.read().decode("utf-8", "replace")
        return o.channel.recv_exit_status(), out, err

    variant = "网络遥控版" if "netrc.py" in files else "纯SBUS原版"
    print("目标 %s@%s:%s  版本=%s  VERSION=%s  源=%s"
          % (USER, host, REMOTE_DIR, variant, version, src))
    if git_dirty(src):
        print("注意: 工作区有未提交改动, VERSION 只记 HEAD(%s), 板上实际代码可能领先该提交" % version)

    # ---- md5 对比(先看差异, --check-only 到此为止) ----
    rc, out, _ = run("md5sum %s" % " ".join(REMOTE_DIR + "/" + f for f in files))
    remote = {}
    for line in out.strip().splitlines():
        p = line.split()
        if len(p) == 2:
            remote[p[1].replace(REMOTE_DIR + "/", "")] = p[0]
    diff = [f for f in files if remote.get(f) != md5(lpath(f))]
    for f in files:
        print(("  %-22s %s" % (f, "一致" if f not in diff else "不同")))
    if check_only:
        print("check-only 结束: %s" % ("全部一致" if not diff else "待更新 %d 个文件" % len(diff)))
        c.close()
        return

    # ---- 上传 + 校验 ----
    # core/ 与 static/ 子目录: sftp.put 不会自动建目录(全新板子首次部署会裸炸)
    run("mkdir -p %s/core %s/static" % (REMOTE_DIR, REMOTE_DIR))
    sftp = c.open_sftp()
    try:
        for f in files:
            sftp.put(str(lpath(f)), REMOTE_DIR + "/" + f)
    except Exception as e:
        # 半途断连: 服务未动(restart 在后面), ext4 未 sync 大概率没落盘,
        # 下次部署 md5 门可拦 —— 直接重跑即可
        sys.exit("上传中断(%s: %r), 服务未动, 直接重跑本脚本" % (type(e).__name__, e))
    if "netrc.py" not in files:
        run("rm -f %s/netrc.py" % REMOTE_DIR)   # 回退原版: 板上残留的 netrc.py 一并清掉
        print("已删板上 netrc.py(回退原版)")
    sftp.close()
    # 板上 ext4 是 commit=600(10分钟才提交日志), 断电会回滚写入 —— sync 强制立即落盘
    run("sync")
    # 校验门(P1-1): md5sum 本身失败(权限/路径)与文件缺失(无输出行)都必须拦下 ——
    # 缺失时 bad 列表看不到该文件, 会谎报"全部一致"
    rc, out, err = run("md5sum %s" % " ".join(REMOTE_DIR + "/" + f for f in files))
    if rc != 0:
        sys.exit("上传后 md5sum 执行失败(rc=%d), 已中止(服务未动):\n%s%s" % (rc, out, err))
    remote = {}
    for line in out.strip().splitlines():
        p = line.split()
        if len(p) == 2:
            remote[p[1].replace(REMOTE_DIR + "/", "")] = p[0]
    missing = [f for f in files if f not in remote]
    bad = [f for f in files if f in remote and remote[f] != md5(lpath(f))]
    if missing or bad:
        sys.exit("上传后校验失败, 已中止(服务未动): 缺失=%s md5不一致=%s"
                 % (missing, bad))
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
            "sudo systemctl daemon-reload && sudo systemctl enable --now go2-audio" % qpw)
        have_audio = run("systemctl cat go2-audio >/dev/null 2>&1")[0] == 0
        if not have_audio:
            sys.exit("go2-audio.service 补装失败, 已中止(音频会丢)")
        print("板上无 go2-audio, 已自动补装")
    rc, _, err = run("echo %s | sudo -S systemctl restart go2-audio" % qpw)   # 先音频后桥(OPTIMIZATION P0-1)
    if rc != 0:
        sys.exit("go2-audio 重启失败(rc=%d), 部署中止:\n%s" % (rc, err.strip()))
    rc, _, err = run("echo %s | sudo -S systemctl restart go2-bridge" % qpw)
    if rc != 0:
        sys.exit("go2-bridge 重启失败(rc=%d), 部署中止(板上仍跑旧代码):\n%s"
                 % (rc, err.strip()))
    # 验活: uvicorn 要等首连 WebRTC 后才起(约 10~20s), 固定 6s 会误判"端口未就绪"
    out = ""
    status = ""
    for _ in range(12):
        time.sleep(2)
        _, status, _ = run("systemctl is-active go2-bridge")
        status = status.strip()
        _, out, _ = run("curl -s --max-time 2 http://127.0.0.1:8000/logs | tail -12")
        if status == "active" and out.strip():
            break
    _, aout, _ = run("systemctl is-active go2-audio")
    print("go2-audio 状态:", aout.strip())
    if aout.strip() != "active":
        sys.exit("!! go2-audio 未激活, 音频不可用(遥控不受影响): 看 journalctl -u go2-audio")
    print("服务状态:", status)
    if status != "active":
        sys.exit("服务未启动(is-active=%r), 部署失败! 先看板上 journalctl" % status)
    rc, out, _ = run("curl -s --max-time 3 http://127.0.0.1:8000/logs | tail -12")
    print("---- /logs 尾部 ----\n" + out)
    if not out.strip():
        sys.exit("服务 active 但 /logs 无内容(端口未就绪/进程异常), 部署判定失败!")

    # ---- /state 冒烟(P1-1): uvicorn 已起后, 10s 内必须拿到 /state ----
    # 硬失败: 服务非 active / /state 拿不到; 子系统异常(link/sbus/audio)只 WARN 不失败。
    st, st_status, deadline = None, "", time.time() + 10
    while time.time() < deadline:
        _, st_status, _ = run("systemctl is-active go2-bridge")
        st_status = st_status.strip()
        _, sout, _ = run("curl -s --max-time 2 http://127.0.0.1:8000/state")
        try:
            st = json.loads(sout)
        except ValueError:
            st = None
        if st_status == "active" and isinstance(st, dict):
            break
        time.sleep(1)
    if st_status != "active":
        sys.exit("服务非 active(is-active=%r), 部署失败! 先看板上 journalctl" % st_status)
    if not isinstance(st, dict):
        sys.exit("重启后 10s 内 /state 不可达或非 JSON(端口未就绪/进程异常), 部署失败!")
    if st.get("ver") != version:
        # 版本化部署门禁核心: restart 没生效/跑的不是刚上传的代码, "烧录完成"就不可信
        sys.exit("版本不符: 板上 /state.ver=%r, 本次部署 VERSION=%r —— 服务没吃到新代码"
                 " (看 journalctl -u go2-bridge)" % (st.get("ver"), version))
    sb = st.get("sbus") or {}
    au = st.get("audio") or {}
    rl = st.get("relay") or {}
    print("---- /state 摘要 ----")
    print("ver=%s fps=%s link=%s relay=%s sbus_ok=%s audio_ready=%s"
          % (st.get("ver"), sb.get("fps"), st.get("link"),
             "on" if rl.get("on") else "off", sb.get("ok"), au.get("ready")))
    warns = []
    if st.get("link") != "up":
        warns.append("link=%r (狗没上电/WebRTC 未连?)" % st.get("link"))
    if sb.get("ok") is not True:
        warns.append("sbus.ok=%r (接收机没开/线没接?)" % sb.get("ok"))
    if au.get("ready") is not True:
        warns.append("audio.ready=%r (看 journalctl -u go2-audio)" % au.get("ready"))

    # ---- 狗 EEE 自检(2026-10-24): 部署时顺带确保狗侧 EEE 关闭(经板子二级跳, best-effort) ----
    # 狗密码: 环境变量 DOG_PW 优先, 否则读本机 credentials.md 的"狗 SSH"条目(需要时读取, 不外传)
    robot_ip = "192.168.123.161"
    try:
        mcf = _re.search(r'^ROBOT_IP\s*=\s*"([^"]+)"',
                         (src / "config.py").read_text(encoding="utf-8"), _re.M)
        if mcf:
            robot_ip = mcf.group(1)
    except Exception:
        pass
    dog_pw = os.environ.get("DOG_PW")
    if not dog_pw:
        try:
            cred = open(os.path.expanduser(r"~/.zcode/credentials.md"), encoding="utf-8").read()
            dog_pw = _re.search(r"狗 SSH：`root` / `([^`]+)`", cred).group(1)
        except Exception:
            dog_pw = None
    if dog_pw:
        dog_eee_guard(c, robot_ip, dog_pw)
    else:
        print("EEE 自检: 无狗密码(DOG_PW 未设且读不到 credentials.md), 跳过")

    c.close()
    if "Traceback" in out:
        sys.exit("启动日志有异常, 人工确认!")
    print("烧录完成 ✓  (App 侧: 对应版本的 PaSend APK 装上 G12 即可)")
    if warns:   # 醒目 WARN 放最后: 子系统异常不判失败, 但不能被"完成"字样盖过去
        print("!" * 64)
        for w in warns:
            print("!! WARN " + w)
        print("!" * 64 + "  代码已部署, 但上述子系统异常, 需人工确认")


if __name__ == "__main__":
    main()
