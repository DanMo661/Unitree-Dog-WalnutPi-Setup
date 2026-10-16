#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SBUS 链路一键诊断（香橙派 go2-bridge）

用法:
  D:\\Work_Project\\ZCodeProject\\rv1106-yolov8\\tools\\py311\\python.exe sbus_diag.py

检查链: 电脑->板网络 -> bridge状态(/ws) -> bridge日志 -> PH0/PH1引脚复用 ->
        ttyS0原始字节(5s) -> 其余串口扫描, 最后汇总判定。
只读诊断, 不改板上任何配置。
"""
import subprocess
import sys
import time

OPI_IP = "192.168.123.50"
DOG_IP = "192.168.123.161"
OPI_USER = "orangepi"
OPI_PW = "orangepi"
PY = r"D:\Work_Project\ZCodeProject\rv1106-yolov8\tools\py311\python.exe"

import paramiko  # noqa: E402
import websocket  # noqa: E402


def ping(ip):
    r = subprocess.run(["ping", "-n", "1", "-w", "1200", ip],
                       capture_output=True, creationflags=0x08000000)
    return r.returncode == 0


def ssh_connect(retries=3):
    last = None
    for i in range(retries):
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(OPI_IP, username=OPI_USER, password=OPI_PW,
                      timeout=8, banner_timeout=15)
            return c
        except Exception as e:
            last = e
            time.sleep(3)
    raise SystemExit(f"[X] SSH 连不上 {OPI_IP}: {last}")


def run(c, cmd, timeout=20):
    _, out, err = c.exec_command(cmd, timeout=timeout)
    return out.read().decode(errors="replace").strip(), err.read().decode(errors="replace").strip()


def ws_status():
    try:
        ws = websocket.create_connection(f"ws://{OPI_IP}:8000/ws", timeout=5)
        msg = ws.recv()
        ws.close()
        return msg
    except Exception as e:
        return f"WS-FAIL: {e}"


def main():
    print("=" * 60)
    print("SBUS 链路诊断", time.strftime("%H:%M:%S"))
    print("=" * 60)

    ok_opi = ping(OPI_IP)
    ok_dog = ping(DOG_IP)
    print(f"[1] 网络: 板{OPI_IP}={'通' if ok_opi else '不通'}  狗{DOG_IP}={'通' if ok_dog else '不通'}")
    if not ok_opi:
        raise SystemExit("[X] 板子 ping 不通: 检查网线/板子是否开机")

    c = ssh_connect()

    # /ws 实时状态
    st = ws_status()
    print(f"[2] bridge实时状态: {st}")

    # bridge 日志尾部
    log, _ = run(c, "journalctl -u go2-bridge -n 8 --no-pager | tail -8")
    print(f"[3] bridge日志尾部:\n{log}")

    # PH0/PH1 引脚复用 (UART0 = ttyS0, pin8=TX pin10=RX)
    mount, _ = run(c, "mount | grep -c debugfs")
    if mount.strip() == "0":
        run(c, f"echo {OPI_PW} | sudo -S mount -t debugfs none /sys/kernel/debug 2>/dev/null")
        time.sleep(1)
    pinmux, err = run(c, f"echo {OPI_PW} | sudo -S sed -n '/pin 224/,/pin 232/p' "
                         f"/sys/kernel/debug/pinctrl/pio/pinmux-pins 2>/dev/null")
    print(f"[4] PH0/PH1引脚复用 (PH1 应为 uart0):")
    print(pinmux if pinmux else f"  读取失败: {err[:120]}")

    # ttyS0 原始监听 5 秒 (容错: bridge 占用时会竞争)
    code = r'''
import serial, time, glob
def sniff(port, secs=2.5):
    try:
        ser = serial.Serial(port=port, baudrate=100000, bytesize=serial.EIGHTBITS,
                            parity=serial.PARITY_EVEN, stopbits=serial.STOPBITS_TWO, timeout=0.3)
    except Exception as e:
        return f"OPEN-FAIL:{str(e)[:50]}", b""
    buf = b""; err = ""
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            n = ser.read(256)
            if n: buf += n
        except Exception as e:
            err = str(e)[:50]; break
    try: ser.close()
    except Exception: pass
    frames = 0; i = 0
    while i < len(buf) - 25:
        if buf[i] == 0x0F: frames += 1; i += 25
        else: i += 1
    tag = f"bytes={len(buf)} frames={frames}"
    if err: tag += f" err={err}"
    elif buf and frames == 0: tag += f" head={buf[:16].hex()}"
    return tag, buf
tag, _ = sniff("/dev/ttyS0", 5)
print("ttyS0:", tag)
for p in ["/dev/ttyS1","/dev/ttyS2","/dev/ttyS3","/dev/ttyS4","/dev/ttyS5"]:
    t, _ = sniff(p, 2)
    print(p + ":", t)
'''
    stdin, stdout, stderr = c.exec_command(
        "/home/orangepi/go2-bridge/.venv/bin/python - <<'EOF'\n" + code + "\nEOF", timeout=60)
    print("[5] 串口原始监听:")
    print(stdout.read().decode(errors="replace").strip())
    se = stderr.read().decode(errors="replace").strip()
    if se:
        print("   stderr:", se[:200])
    c.close()

    print("=" * 60)
    print("判定指引:")
    print("  ttyS0 frames>0        -> 信号正常, 问题在bridge侧(极少见)")
    print("  ttyS0 bytes=0 + PH1是uart0 -> 信号没接对引脚(板端RX=40pin第10脚)")
    print("  ttyS0 bytes=0 + PH1是gpio  -> 引脚复用被切走, 需修设备树/overlay")
    print("  某个ttySx frames>0    -> SBUS线插错串口了, 告诉我是哪个")
    print("=" * 60)


if __name__ == "__main__":
    main()
