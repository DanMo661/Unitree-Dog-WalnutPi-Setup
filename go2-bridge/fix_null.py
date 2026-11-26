# -*- coding: utf-8 -*-
"""one-off: 修 junk 行被压成真字节的转义。"""
p = "test_gas.py"
b = open(p, "rb").read()
target = b'b"\xc3\xbf\x01\x03\x14" + GOLD'          # 真字节版(ÿ 为 UTF-8 双字节)
fixed = b'b"\\xff\\x01\\x03\\x14" + GOLD'
assert target in b, "junk literal not found"
b = b.replace(target, fixed)
open(p, "wb").write(b)
# 全文件扫描: 引号内可疑高位字节再报一次
import ast
ast.parse(b.decode("utf-8"))
print("fixed + parses clean")
