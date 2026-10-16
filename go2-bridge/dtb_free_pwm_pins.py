# -*- coding: utf-8 -*-
"""dtb 补丁: 删掉指定 pwm 通道子节点的 pinctrl 声明, 把对应引脚释放为普通 IO.
不动 pwm5(PA12=ac200 时钟, WiFi 相关)和 pwm0(无引脚). 原dtb保留, 生成新文件.
用法: sudo python3 dtb_free_pwm_pins.py <源dtb> <目标dtb> <通道串>
例: sudo python3 dtb_free_pwm_pins.py /boot/sun50i-h616-walnutpi-1b.dtb \
       /boot/sun50i-h616-walnutpi-1b-pwm12.dtb 34   # 只释放pwm3/4, 保留pwm1/2"""
import re
import subprocess
import sys

src, dst, chans = sys.argv[1], sys.argv[2], sys.argv[3]
assert re.fullmatch(r"[1-4]+", chans), "通道串只能由1~4组成"
keep = [c for c in "1234" if c not in chans]      # 保留 pinctrl 的通道(校验用)
dts = subprocess.run(['dtc', '-I', 'dtb', '-O', 'dts', src],
                     capture_output=True, text=True).stdout
if not dts:
    sys.exit('反编译失败')

pat = re.compile(r'\s*pwm[%s]@0300a000 \{' % chans)
out, inblock, removed = [], False, 0
for line in dts.splitlines():
    if pat.match(line):
        inblock = True
    elif inblock and line.strip() == '};':
        inblock = False
    if inblock and 'pinctrl' in line:
        removed += 1
        continue
    out.append(line)
print('删除 pinctrl 行数:', removed)
assert removed == 2 * len(set(chans)), '行数不符, 中止不改'

open('/tmp/_patch.dts', 'w').write('\n'.join(out) + '\n')
r = subprocess.run(['dtc', '-I', 'dts', '-O', 'dtb', '-o', dst, '/tmp/_patch.dts'],
                   capture_output=True, text=True)
print('编译 rc=%d %s' % (r.returncode, (r.stderr or '')[-200:]))

chk = subprocess.run(['dtc', '-I', 'dtb', '-O', 'dts', dst],
                     capture_output=True, text=True).stdout
for c in set(chans):
    blk = re.search(r'pwm%s@0300a000 \{(.*?)\};' % c, chk, re.S)
    assert blk and 'pinctrl' not in blk.group(1), 'pwm%s 仍有 pinctrl' % c
for c in keep:
    blk = re.search(r'pwm%s@0300a000 \{(.*?)\};' % c, chk, re.S)
    assert blk and 'pinctrl' in blk.group(1), 'pwm%s 的 pinctrl 丢了, 中止!' % c
blk5 = re.search(r'pwm5@0300a000 \{(.*?)\};', chk, re.S)
assert blk5 and 'pinctrl' in blk5.group(1), 'pwm5 的 pinctrl 丢了, 中止!'
print('校验通过: %s (pwm%s 已释放, pwm%s/pwm5 保留)' % (dst, chans, ''.join(keep)))
