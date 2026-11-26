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
        self.cur_us = None
        self.level = None
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

    def set_us(self, us):
        """直接按脉宽输出(us), 供 RC-PWM 解码型继电器(量脉宽不量电平);
        变化小于 25us 不写盘; 每跨 250us 记一条日志"""
        if not self.ok:
            return
        us = max(float(self.min_us), min(float(self.max_us), us))
        if self.cur_us is not None and abs(us - self.cur_us) < 25.0:
            return
        try:
            self._w("duty_cycle", int(us * 1000))
            if self.cur_us is None or int(us // 250) != int(self.cur_us // 250):
                self.log("%s: 脉宽 -> %dus" % (self.dir.rsplit("/", 1)[-1], round(us)))
            self.cur_us = us
        except Exception as e:
            self.log("脉宽写入失败, 已停用:", repr(e))
            self.ok = False

    def set_level(self, on):
        """恒定电平输出: True=常高(duty=period), False=常低(duty=0)
        供逻辑电平型外设(继电器模块); 只在电平变化时写盘"""
        if not self.ok or self.level == on:
            return
        try:
            self._w("duty_cycle", self.period_ns if on else 0)
            self.level = on
            self.log("%s: 电平 -> %s" % (self.dir.rsplit("/", 1)[-1],
                                         "高(继电器开)" if on else "低(继电器关)"))
        except Exception as e:
            self.log("电平写入失败, 已停用:", repr(e))
            self.ok = False


class RelayPwm:
    """B 键继电器双路并出(2026-10-24 统一两台狗): PWM1+PWM2 同值同时输出。
    dog2 的航模继电器接 pwm2(16脚) / dog3 接 pwm1(15脚), 双路并出后一份代码通吃。
    对外只暴露 set_us/cur_us(与 Servo 同接口, rc_loop/影子/FsmActive 复用);
    单路 export 失败由 Servo 自身降级空转, 另一路照常工作。"""

    def __init__(self, outs):
        self.outs = outs        # [Servo(pwm1), Servo(pwm2)]

    @property
    def cur_us(self):
        return self.outs[0].cur_us   # 两路同值写, 取第一路代表(仅写成功后更新)

    @property
    def level(self):
        return self.outs[0].level

    def set_us(self, us):
        for s in self.outs:
            s.set_us(us)


class GpioOut:
    """sysfs GPIO 恒电平输出(继电器直控, 2026-10-10 定稿; 比 PWM 更直接)
    gpio 号 = gpiochip0 base(0) + 全局引脚号: PI3=259(35脚) PI4=260(37脚) PI14=270(40脚)
    打不开时打日志降级空转, 不影响桥其他功能"""

    def __init__(self, gpio_num, log=print):
        self.log = log
        self.gpio_num = gpio_num
        self.level = None
        self.ok = False
        self.dir = "/sys/class/gpio/gpio%d" % gpio_num
        try:
            if not os.path.exists(self.dir):
                with open("/sys/class/gpio/export", "w") as f:
                    f.write(str(gpio_num))
            self._w("direction", "out")
            self._w("value", 0)
            self.level = False
            self.ok = True
            log("GPIO 就绪: %s (上电=低/继电器关)" % self.dir)
        except Exception as e:
            log("GPIO 不可用, 已降级空转:", repr(e))

    def _w(self, name, v):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(str(v))

    def set_level(self, on):
        """电平输出: True=高(继电器吸合), False=低(释放); 只在变化时写盘"""
        if not self.ok or self.level == on:
            return
        try:
            self._w("value", 1 if on else 0)
            self.level = on
            self.log("GPIO%d: 电平 -> %s" % (self.gpio_num,
                                             "高(继电器开)" if on else "低(继电器关)"))
        except Exception as e:
            self.log("GPIO 写入失败, 已停用:", repr(e))
            self.ok = False
