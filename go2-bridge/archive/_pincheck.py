# -*- coding: utf-8 -*-
"""临时诊断: 板子身份 + PWM 引脚功能核对"""
import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('192.168.123.50', username='pi', password='pi', timeout=15)


def run(cmd, t=25):
    _, o, e = c.exec_command(cmd, timeout=t)
    return o.read().decode('utf-8', 'replace') + e.read().decode('utf-8', 'replace')


print('=== 板子身份 ===')
print(run('cat /sys/firmware/devicetree/base/model; echo; tr "\\0" " " < /proc/device-tree/compatible; echo'))
print('=== 所有 pwm 功能组(完整) ===')
print(run('echo pi | sudo -S sh -c \'cat /sys/kernel/debug/pinctrl/*/pinmux-functions | grep -i pwm\''))
print('=== 含 PI3/4/5/11/12/13/14 的功能行 ===')
print(run('echo pi | sudo -S sh -c \'cat /sys/kernel/debug/pinctrl/*/pinmux-functions | grep -wE ".*PI3 |.*PI4 |.*PI5 |.*PI11 |.*PI12 |.*PI13 |.*PI14 "\''))
print('=== 这些脚的当前复用 ===')
print(run('echo pi | sudo -S sh -c \'cat /sys/kernel/debug/pinctrl/*/pinmux-pins | grep -E "\\(PI3\\)|\\(PI4\\)|\\(PI5\\)|\\(PI11\\)|\\(PI12\\)|\\(PI13\\)|\\(PI14\\)"\''))
print('=== 所有 pwmchip ===')
print(run('ls /sys/class/pwm/'))
c.close()
