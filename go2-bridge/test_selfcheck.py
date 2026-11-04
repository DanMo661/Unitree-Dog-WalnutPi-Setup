# -*- coding: utf-8 -*-
"""test_selfcheck.py -- 开机自检(selfcheck.py)离线测试: 假 runner 注入。
用法: python run_test_shim.py test_selfcheck.py
"""
import sys

FAILS = []


def ok(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


sys.path.insert(0, ".")
import selfcheck  # noqa: E402


class FakeState:
    sbus_ok = True
    link = "up"


import tempfile  # noqa: E402
_TMP = tempfile.mkdtemp(prefix="selfcheck_test_")


def run_all(outputs, **kw):
    """构造假 runner(命令->输出映射, 未命中返回 ERR)后跑全部检查。
    base_dir 用临时目录(disk 探测), 开发机上没有 /home/pi。"""
    def runner(cmd, t=10):
        for k, v in outputs.items():
            if k in cmd:
                return v
        return ""
    kw.setdefault("state", FakeState())
    kw.setdefault("base_dir", _TMP)
    return selfcheck.run_checks(runner, **kw)


# ---- 1. 全绿场景 ----
GOOD = {
    "addr show wlan0": "wlan0 UP 192.168.8.54/24",
    "iw dev wlan0 link": "connected 62:31:fa:8b:e4:c0\n\tSSID: 尧祐智能",
    "con show": "home\nhotspot\nlo",
    "is-active wifi_watchdog": "active",
    "carrier": "",                      # _check_eth0 直接读文件, runner 不经手
    "is-active serial-getty@ttyS0": "inactive",
    "is-active go2-audio": "active",
    "is-active go2-pa-page": "active",
}
res = run_all(GOOD)
ok(res["wlan0"].startswith("ok:") and "192.168.8.54" in res["wlan0"]
   and "尧祐智能" in res["wlan0"], "wlan0: 地址+SSID 都在 -> ok")
ok(res["nm_home"].startswith("ok"), "nm home 档案存在")
ok(res["wifi_watchdog"].startswith("ok"), "看门狗 active")
ok(res["disk"].startswith("ok"), "磁盘可写(开发机上探测)")
ok(res["svc_audio"].startswith("ok") and res["svc_pa_page"].startswith("ok"),
   "双服务 active")
ok(res["sbus"].startswith("ok") and res["dog"].startswith("ok"),
   "SBUS/狗链路(镜像 state)")

# ---- 2. WiFi 全灭场景(烧卡后忘跑 board_wifi_setup 的复现) ----
res = run_all({
    "addr show wlan0": "wlan0 DOWN",
    "con show": "",
    "is-active wifi_watchdog": "",
})
ok(res["wlan0"].startswith("bad:"), "WiFi 没起来 -> bad")
ok(res["nm_home"].startswith("bad:") and "无 home 档案" in res["nm_home"],
   "home 档案丢失 -> bad(正是 11-04 实际事故)")
ok(res["wifi_watchdog"].startswith("bad:"), "看门狗服务不存在 -> bad")

# ---- 3. gas getty 占用(仅 GAS_ENABLED 时才查) ----
res = run_all(dict(GOOD, **{"is-active serial-getty@ttyS0": "active"}),
              gas_enabled=True)
ok(res["gas_getty"].startswith("bad:") and "占用" in res["gas_getty"],
   "getty 占用 ttyS0 -> bad")
ok("gas_getty" not in run_all(GOOD), "GAS_ENABLED=False 时不查 gas_getty")

# ---- 4. EEE 标记: 开发机无标记 -> skip 语义 ----
res = run_all(GOOD)
ok(res["eee_marker"].startswith("skip") or res["eee_marker"].startswith("ok"),
   "EEE 标记: 无标记记 skip(部署守卫负责), 不算 bad")

# ---- 5. emit: 日志行与返回的 bad 集合 ----
bad = selfcheck.emit(res, journal_enabled=False)
ok(isinstance(bad, dict), "emit 返回 bad 集合")

# ---- 6. eth0 载波检查(直读 sysfs, 开发机上断言不炸即可) ----
res2 = selfcheck.run_checks(lambda cmd: "", FakeState())
ok("eth0" in res2, "eth0 检查存在(结果依机器而异)")

# ---- 7. run_later 全路径回归(11-04: 残留旧形参 TypeError 线程静默死亡, 测试没拦) ----
from telemetry import _LOG_BUF  # noqa: E402
n0 = len(_LOG_BUF)
selfcheck.run_later(delay=0, state=FakeState(), journal_enabled=False,
                    runner=lambda cmd, t=10: "active")
ok(len(_LOG_BUF) > n0 and any("开机自检" in l for l in list(_LOG_BUF)[n0:]),
   "run_later 全路径跑完并 emit 日志")
ok(not any("线程异常" in l for l in list(_LOG_BUF)[n0:]), "run_later 无异常")

# ---- 8. 自检自身挂掉必须喊(不能静默) ----
# 直接复现 11-04 现场病: run_checks 签名漂移 -> TypeError -> 线程静默死亡
def broken(*a, **kw):
    raise TypeError("run_checks() got an unexpected keyword argument 'dog_state'")
errs = []
_orig_checks = selfcheck.run_checks
selfcheck.run_checks = broken
try:
    selfcheck.run_later(delay=0, state=FakeState(), journal_enabled=False,
                        runner=lambda cmd, t=10: "active",
                        log_fn=lambda *a, **k: errs.append(" ".join(map(str, a))))
finally:
    selfcheck.run_checks = _orig_checks
ok(any("自检线程异常" in x and "TypeError" in x for x in errs),
   "run_checks 炸 -> error 日志(不静默, 11-04 事故回归)")

print()
if FAILS:
    print("失败 %d 项: %s" % (len(FAILS), FAILS))
    raise SystemExit(1)
print("selfcheck 全部通过")
