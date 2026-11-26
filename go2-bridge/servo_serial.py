# -*- coding: utf-8 -*-
"""串口→舵机云台桥: 收 RV1106 的调摆量, 驱动核桃派双路硬件 PWM
协议(115200, ASCII 行): "T <pan_deg> <tilt_deg> <lock> <frame>"
  非T开头行丢弃(调试串口共口时的控制台噪音)
PWM: pwmchip0  pan=pwm3(pin38=PI13)  tilt=pwm1(pin15=PI11), 50Hz, 500~2500us=0~180°
角度映射: pan/tilt ±90°(tilt ±45 由 RV 侧限) -> 舵机角 = 90 + 角度
看门狗: 2s 无有效行则保持最后脉宽(安全=不动)
用法: python3 servo_serial.py [uart]   默认 /dev/ttyS2   (root 跑: sysfs pwm)
"""
import os
import serial
import sys
import time

UART = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyS2"
PWM_CHIP = "/sys/class/pwm/pwmchip0"
PERIOD_NS = 20_000_000
MIN_US, MAX_US = 500, 2500
SERVO_RANGE = 180.0
CHANNELS = {"pan": 3, "tilt": 1}          # pwm3=pin38, pwm1=pin15


class PwmOut:
    def __init__(self, chip, ch, log=print):
        self.dir = os.path.join(chip, "pwm%d" % ch)
        try:
            if not os.path.exists(self.dir):
                with open(os.path.join(chip, "export"), "w") as f:
                    f.write(str(ch))
            for _ in range(5):
                try:
                    with open(os.path.join(self.dir, "period"), "w") as f:
                        f.write(str(PERIOD_NS))
                    with open(os.path.join(self.dir, "duty_cycle"), "w") as f:
                        f.write(str(self.us_to_ns(90)))
                    with open(os.path.join(self.dir, "enable"), "w") as f:
                        f.write("1")
                    break
                except OSError:
                    time.sleep(0.4)
            else:
                raise OSError("pwm%d 写入失败" % ch)
            log("PWM 就绪: %s (中位 90°)" % self.dir)
        except Exception as e:
            log("PWM 不可用(降级空转): %r" % e)
            self.dir = None

    @staticmethod
    def us_to_ns(deg):
        deg = max(0.0, min(SERVO_RANGE, deg))
        return int((MIN_US + (MAX_US - MIN_US) * deg / SERVO_RANGE) * 1000)

    def set_deg(self, deg):
        if not self.dir:
            return
        with open(os.path.join(self.dir, "duty_cycle"), "w") as f:
            f.write(str(self.us_to_ns(deg)))


def main():
    print("== servo_serial 桥启动 uart=%s ==" % UART, flush=True)
    pwms = {k: PwmOut(PWM_CHIP, ch) for k, ch in CHANNELS.items()}
    ser = serial.Serial(UART, 115200, timeout=0.5)
    print("串口就绪: %s@115200, 等 RV 的 T 行..." % UART, flush=True)
    last_t = time.time()
    buf = b""
    n_lines = n_t = 0
    while True:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                n_lines += 1
                s = line.strip().decode("ascii", "ignore")
                if not s.startswith("T "):
                    continue
                p = s.split()
                if len(p) < 4:
                    continue
                try:
                    pan, tilt, lock = float(p[1]), float(p[2]), int(p[3])
                except ValueError:
                    continue
                pwms["pan"].set_deg(90 + pan)
                pwms["tilt"].set_deg(90 + tilt)
                last_t = time.time()
                n_t += 1
                if n_t % 600 == 1:
                    print("T行 pan=%.1f tilt=%.1f lock=%d (有效%d/总%d)"
                          % (pan, tilt, lock, n_t, n_lines), flush=True)
        if time.time() - last_t > 30 and n_t == 0:
            print("...仍在等首个 T 行 (收到 %d 行噪音)" % n_lines, flush=True)
            last_t = time.time()


if __name__ == "__main__":
    main()
