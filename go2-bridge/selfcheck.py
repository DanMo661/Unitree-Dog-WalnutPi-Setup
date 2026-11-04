# -*- coding: utf-8 -*-
"""selfcheck.py -- 开机子系统自检(2026-11-04 加, 起因: 烧卡后 WiFi 配置丢了没人发现)。

启动 ~25s 后跑一次(给 WiFi 关联/狗链路留稳定时间), 结果逐行吐日志(控制台=网页日志
面板可见)+一条 journal 事件存档。只读检查, 任何一步失败不影响运行。

检查项: wlan0 关联与地址 / NM 的 home 档案存在 / wifi_watchdog 服务 / eth0 载波 /
磁盘可写(防只读自保静默) / gas 串口被 getty 占用(仅 GAS_ENABLED 时) / go2-audio /
go2-pa-page / SBUS 帧 / 狗链路 / EEE 部署标记(狗侧实际状态由 deploy_pc 守卫负责)。
"""
import os
import subprocess
import time

import config
import journal
from telemetry import log, _ev

MARKER = "/home/pi/go2-bridge/.eee_ok"   # deploy_pc EEE 守卫成功时落的标记
_DELAY = 25.0


def _sh(cmd, t=10):
    """runner: 板上执行命令返回合并输出(桥以 root 跑, 无需 sudo)。"""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, timeout=t)
        return (p.stdout + p.stderr).decode("utf-8", "replace").strip()
    except Exception as e:
        return "ERR:" + repr(e)


def _check_wlan(run):
    out = run("ip -4 -br addr show wlan0")
    ip = ""
    for tok in out.split():
        if tok.count(".") == 3:
            ip = tok
    ssid_out = run("iw dev wlan0 link")
    ssid = ""
    for line in ssid_out.splitlines():
        if "SSID:" in line:
            ssid = line.split("SSID:")[1].strip()
    ok = bool(ip) and bool(ssid)
    return ok, ("%s (%s)" % (ip, ssid) if ssid else ip or out or "无地址未关联")


def _check_nm_home(run):
    out = run("nmcli -t -f NAME con show")
    return "home" in out.split(), ("home 档案在" if "home" in out.split()
                                   else "无 home 档案(NM 连接列表: %s)" % (out or "空"))


def _check_watchdog(run):
    out = run("systemctl is-active wifi_watchdog")
    return out == "active", out or "服务不存在"


def _check_eth0(run):
    try:
        with open("/sys/class/net/eth0/carrier") as f:
            v = f.read().strip()
        return v == "1", ("载波正常" if v == "1" else "载波丢失(%s)" % v)
    except Exception as e:
        return None, repr(e)


def _check_disk(run, base_dir):
    probe = os.path.join(base_dir, ".rw_probe")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True, "可写"
    except Exception as e:
        return False, "不可写!(只读自保?) %r" % e


def _check_gas_getty(run, port):
    dev = os.path.basename(port)
    out = run("systemctl is-active serial-getty@%s" % dev)
    return out != "active", ("未被占用" if out != "active"
                             else "serial-getty 占用 %s, 传感器读不到!" % dev)


def _check_svc(name):
    def _f(run):
        out = run("systemctl is-active %s" % name)
        return out == "active", out or "服务不存在"
    return _f


def _check_eee_marker(run, base_dir):
    path = os.path.join(base_dir, ".eee_ok")
    try:
        with open(path) as f:
            ts = f.read().strip()
        return True, "部署时已确认 disabled(标记 %s)" % (ts or "无时间")
    except Exception:
        return None, "无部署标记(狗侧实际状态由 deploy_pc 尾部守卫负责)"


def run_checks(run, state, base_dir="/home/pi/go2-bridge", gas_enabled=False):
    """跑全部检查, 返回 {名字: 'ok[:detail]' / 'bad:detail' / 'skip:detail'}。
    runner 可注入(测试用假 runner); state 用 telemetry.state(真机)。"""
    res = {}

    def add(name, ok, detail):
        res[name] = ("ok" if ok else ("skip" if ok is None else "bad"))
        if detail:
            res[name] += ":" + detail

    checks = [
        ("wlan0", lambda: _check_wlan(run)),
        ("nm_home", lambda: _check_nm_home(run)),
        ("wifi_watchdog", lambda: _check_watchdog(run)),
        ("eth0", lambda: _check_eth0(run)),
        ("disk", lambda: _check_disk(run, base_dir)),
        ("svc_audio", lambda: _check_svc("go2-audio")(run)),
        ("svc_pa_page", lambda: _check_svc("go2-pa-page")(run)),
        ("eee_marker", lambda: _check_eee_marker(run, base_dir)),
    ]
    if gas_enabled:
        gp = getattr(config, "GAS_PORT", "/dev/ttyS3")
        checks.append(("gas_getty", lambda: _check_gas_getty(run, gp)))
    for name, f in checks:
        try:
            ok, detail = f()
        except Exception as e:
            ok, detail = None, repr(e)
        add(name, ok, str(detail))
    # 控制面两件套: 取自 state 镜像(启动 25s 后应已稳定)
    add("sbus", bool(state.sbus_ok), "fps 见 /state" if state.sbus_ok else "无帧流")
    add("dog", state.link == "up", "link=%s" % state.link)
    return res


_LAST = {"ts": None, "items": {}, "bad_n": None}   # 最近一次结果(/state.selfcheck 读)


def last_snapshot():
    return dict(_LAST)


def emit(res, journal_enabled=True):
    """结果逐行进日志 + 一条 journal 事件。"""
    global _LAST
    _LAST = {"ts": time.time(), "items": dict(res),
             "bad_n": sum(1 for v in res.values() if v.startswith("bad"))}
    bad = {k: v for k, v in res.items() if v.startswith("bad")}
    log("---- 开机自检 (%d 项, %d 异常) ----" % (len(res), len(bad)))
    for k, v in res.items():
        mark = "✓" if v.startswith("ok") else ("△" if v.startswith("skip") else "✗")
        log("自检[%s] %s %s" % (k, mark, v.split(":", 1)[1] if ":" in v else ""),
            lvl="" if not v.startswith("bad") else "warn")
    if journal_enabled:
        journal.record("selfcheck", **res)
        _ev("sys", "selfcheck", "%d项/%d异常" % (len(res), len(bad)))
    return bad


def run_later(delay=None, base_dir="/home/pi/go2-bridge", state=None,
              journal_enabled=True, runner=None, log_fn=None):
    """线程入口: 等待后自检并吐结果。bridge.main 里 daemon 线程启动。
    log_fn: 异常输出钩子(默认 telemetry.log; 测试注入)。"""
    logf = log_fn or log
    d = float(delay) if delay is not None else float(
        getattr(config, "SELFCHECK_DELAY_S", _DELAY))
    time.sleep(d)
    run = runner or _sh
    try:
        res = run_checks(run, state, base_dir=base_dir,
                         gas_enabled=bool(getattr(config, "GAS_ENABLED", False)))
        emit(res, journal_enabled)
    except Exception as e:
        # 自检自身挂了必须大声说, 不许静默(11-04: run_later 残留旧形参 TypeError,
        # 线程异常默认静默, 挂了一下午没人知道)
        logf("开机自检线程异常(自检未完成):", repr(e), lvl="error")
