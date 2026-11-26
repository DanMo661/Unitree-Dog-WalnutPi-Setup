# -*- coding: utf-8 -*-
"""uboot_bootdelay.py -- 一次性把 U-Boot bootdelay 置 0, 让串口0 对开机杂波免疫。

背景(2026-11-04 dog4 现场定位): 串口0(CPU-U0)是 CPU 启动控制台, U-Boot autoboot
倒计时期间监听该口, 传感器上电杂波会打断 autoboot -> 板子卡 U-Boot -> 网络死。
bootdelay=0 后 U-Boot 不再监听, 传感器常插不再影响启动。

用法(PC 上, USB-TTL 接板子 CPU-U0, 交叉: 适配器TX->板RX/适配器RX<-板TX):
    python tools/uboot_bootdelay.py --port COM5
然后按提示给板子断电重启, 脚本自动抓 autoboot 窗口 -> setenv bootdelay 0 -> saveenv -> reset。
若板侧有 fw_setenv(Linux 里直接改), 优先用那个, 本脚本是串口备用通道。
"""
import argparse
import time

import serial


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM5")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--timeout", type=float, default=150.0,
                    help="等 U-Boot 窗口的总秒数(覆盖多次重启尝试)")
    a = ap.parse_args()

    s = serial.Serial(a.port, a.baud, timeout=0.2)
    print("串口 %s@%d 已开。请现在给板子断电再上电(或按 RESET)..." % (a.port, a.baud))
    print("脚本将每 0.35s 发一个回车抢 autoboot 窗口, 最长等 %.0fs\n" % a.timeout)

    buf = b""
    t0 = time.time()
    in_uboot = False
    steps = ["setenv bootdelay 0\r", "saveenv\r", "printenv bootdelay\r", "reset\r"]
    step_i = 0
    last_send = 0.0
    done = False

    while time.time() - t0 < a.timeout:
        chunk = s.read(512)
        if chunk:
            buf += chunk
            tail = buf[-400:].decode("utf-8", "replace")
            if "Starting kernel" in tail or "Booting Linux" in tail:
                print("\n!! 内核已启动 = 错过了 U-Boot 窗口(传感器还插着?), 请拔掉传感器重新上电再试")
                break
        now = time.time()
        if not in_uboot:
            if now - last_send >= 0.35:
                s.write(b"\r\n")
                last_send = now
            tail = buf[-300:].decode("utf-8", "replace")
            if ("Hit any key" in tail or "stop autoboot" in tail
                    or tail.rstrip().endswith("=>")):
                in_uboot = True
                print("\n[+] 进入 U-Boot! 开始下发命令...")
                time.sleep(0.3)
                buf = b""                       # 清缓冲, 从头按步骤走
                continue
        else:
            if step_i < len(steps) and now - last_send >= 0.6:
                cmd = steps[step_i]
                s.write(cmd.encode())
                print("[>] %s" % cmd.strip())
                step_i += 1
                last_send = now
                if cmd.strip() == "reset":
                    done = True
                    print("\n[✓] 完成: bootdelay=0 已写入并重启。传感器可以常插了。")
                    print("    验证: 板子起网后插拔传感器任意折腾, 网络都该正常起来。")
                    break
    s.close()
    if not done:
        print("\n[x] 未完成: 确认 1)接线交叉(TX<->RX) 2)共地 3)波特率 115200 4)板子真的重启了")
    print("\n---- 现场回放(尾 1200 字符) ----")
    # buf 在循环里被清过, 这里重印不可能; 原始回放靠终端滚动记录, 故只提示


if __name__ == "__main__":
    main()
