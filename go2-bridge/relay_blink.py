# -*- coding: utf-8 -*-
"""继电器链路隔离测试: 六个输出脚每 1s 一起翻转高低(吸合1s/释放1s), 与 SBUS/按键完全无关.
目的: 用户不确定故障在哪一段——继电器若跟节奏咔嗒 => 输出侧全好(问题在按键/通道链);
      完全不动 => 接线/继电器/共地侧问题.
跑法(板子, root): sudo systemd-run --unit=relay-blink --collect \
                     --property=TimeoutStopSec=180 \
                     python3 /home/pi/go2-bridge/relay_blink.py
行为: 自动停 go2-bridge 防抢占; 600s 后或被 systemctl stop/中断, 尝试全脚归低并自动重启桥.
      桥重启后接管这些脚, 继电器可能再动一下属正常.
注意: kill -9 不会自愈(手动恢复: sudo systemctl start go2-bridge);
      重复启动需先 systemctl stop relay-blink; 测试期间勿部署/勿手动启动 go2-bridge."""
import os
import signal
import subprocess
import sys
import time

PINS = {267: "15脚PI11", 268: "16脚PI12", 269: "38脚PI13",
        270: "40脚PI14", 260: "37脚PI4", 259: "35脚PI3"}
GPIO_BASE = "/sys/class/gpio"
DURATION = 600          # 测试窗口(秒)
TOGGLE_S = 1.0          # 每 1s 翻转一次(吸合1s/释放1s)


def warn(*a):
    print(*a, file=sys.stderr, flush=True)


def ensure(n):
    d = os.path.join(GPIO_BASE, "gpio%d" % n)
    if not os.path.exists(d):
        with open(os.path.join(GPIO_BASE, "export"), "w") as f:
            f.write(str(n))
    p = os.path.join(d, "direction")
    cur = ""
    try:
        with open(p) as f:
            cur = f.read().strip()
    except OSError:
        pass
    if cur != "out":                     # 已是 out 不重写, 防输出毛刺
        with open(p, "w") as f:
            f.write("out")
    return d


def svc(*args, timeout=60):
    try:
        r = subprocess.run(["systemctl"] + list(args), capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        warn("警告: systemctl %s 超时 %ds" % (" ".join(args), timeout))
        return
    if r.returncode != 0:
        warn("警告: systemctl %s 失败(rc=%d): %s"
             % (" ".join(args), r.returncode, (r.stderr or "").strip()))


def bridge_stopped():
    """桥真的停下来没有(inactive/failed 都算停: 不会再写脚)"""
    try:
        r = subprocess.run(["systemctl", "is-active", "go2-bridge"],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() in ("inactive", "failed")
    except subprocess.TimeoutExpired:
        return False


_stop = False


def _on_term(signum, frame):
    global _stop
    _stop = True


def main():
    if os.geteuid() != 0:
        sys.exit("需要 root(操作 /sys/class/gpio 与 systemctl), 用 systemd-run 或 sudo 跑")
    if not PINS:
        sys.exit("PINS 为空, 无可测试脚")
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    svc("stop", "go2-bridge")            # 防桥同时写这些脚
    dirs = {}
    try:
        if not bridge_stopped():
            sys.exit("go2-bridge 停不下来, 放弃测试防双写(桥应仍在运行)")
        for n, name in PINS.items():     # 逐脚累存: 部分失败也保留已成功的
            try:
                dirs[n] = ensure(n)
            except Exception as e:
                warn("警告: %s(gpio%d) 配置失败: %r" % (name, n, e))
        if not dirs:
            sys.exit("没有可用的输出脚")
        print("就绪 %d 脚: %s" % (len(dirs), ", ".join(PINS[n] for n in dirs)), flush=True)
        t0 = time.monotonic()
        next_t = t0                      # 绝对时间调度, 防累计漂移
        i = 0
        while not _stop and t0 + DURATION > time.monotonic():
            now = time.monotonic()
            if next_t > now:
                time.sleep(next_t - now)
            now = time.monotonic()
            if next_t <= now:            # 到点/落后: 对齐到未来最近网格(跳拍保相位)
                skip = 0
                while next_t <= now:
                    next_t += TOGGLE_S
                    skip += 1
                if skip > 1:
                    warn("警告: 系统卡顿, 跳过 %d 拍" % (skip - 1))
            v = i % 2
            dead = []
            for n, d in dirs.items():    # 单脚写失败剔除该脚, 不杀整个测试
                try:
                    with open(os.path.join(d, "value"), "w") as f:
                        f.write(str(v))
                except OSError as e:
                    warn("警告: gpio%d 写失败, 剔除: %r" % (n, e))
                    dead.append(n)
            for n in dead:
                del dirs[n]
            if not dirs:
                sys.exit("所有输出脚都写失败了")
            if i % 10 == 0:
                print("%s 第%d拍 全脚=%d" % (time.strftime("%H:%M:%S"), i, v), flush=True)
            i += 1
    finally:
        bad = []
        for n, d in dirs.items():
            try:
                with open(os.path.join(d, "value"), "w") as f:
                    f.write("0")
            except OSError:
                bad.append(str(n))
        msg = ("全脚归低" if dirs and not bad
               else ("gpio%s 归零失败!" % ",".join(bad)) if bad else "无脚被驱动")
        print("结束: %s, 重启 go2-bridge 恢复正常控制" % msg, flush=True)
        svc("start", "go2-bridge", timeout=20)


if __name__ == "__main__":
    main()
