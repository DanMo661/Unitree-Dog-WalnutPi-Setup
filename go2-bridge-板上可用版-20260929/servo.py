# -*- coding: utf-8 -*-
"""PI13(PWM3) 舵机控制: sysfs 硬件 PWM, 50Hz, 脉宽 min_us~max_us 对应 0~180°
打不开(未使能/无权限)时打日志降级空转, 不影响桥其他功能"""
import os
import time


class Servo:
    def __init__(self, chip, channel, min_us, max_us, log=print):
        self.log = log
        self.min_us, self.max_us = min_us, max_us
        self.period_ns = 20_000_000            # 50Hz
        self.cur = None
        self.ok = False
        self.chip_dir = "/sys/class/pwm/pwmchip%d" % chip
        self.dir = os.path.join(self.chip_dir, "pwm%d" % channel)
        try:
            if not os.path.exists(self.dir):
                with open(os.path.join(self.chip_dir, "export"), "w") as f:
                    f.write(str(channel))
            for _ in range(4):                 # udev 改属主可能慢半拍, 重试等权限
                try:
                    self._w("period", self.period_ns)
                    self._w("duty_cycle", (self.min_us + self.max_us) // 2 * 1000)
                    self._w("enable", 1)
                    self.ok = True
                    break
                except PermissionError:
                    time.sleep(0.5)
            if self.ok:
                log("舵机就绪: %s (脉宽 %d~%dus = 0~180°, 上电中位 90°)"
                    % (self.dir, min_us, max_us))
            else:
                log("舵机无权限, 已降级空转:", self.dir)
        except Exception as e:
            log("舵机不可用, 已降级空转:", repr(e))

    def _w(self, name, v):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(str(v))

    def set_angle(self, deg):
        """0~180° 等比例; 变化小于 1° 不写盘(20Hz 循环防刷 sysfs); 每跨 10° 记一条日志"""
        if not self.ok:
            return
        deg = max(0.0, min(180.0, deg))
        if self.cur is not None and abs(deg - self.cur) < 1.0:
            return
        us = self.min_us + (self.max_us - self.min_us) * deg / 180.0
        try:
            self._w("duty_cycle", int(us * 1000))
            if self.cur is None or int(deg // 10) != int(self.cur // 10):
                self.log("舵机 -> %d°" % round(deg))
            self.cur = deg
        except Exception as e:
            self.log("舵机写入失败, 已停用:", repr(e))
            self.ok = False
