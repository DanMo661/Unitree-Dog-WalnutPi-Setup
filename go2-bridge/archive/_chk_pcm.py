# -*- coding: utf-8 -*-
"""校验 /root/pa-files/*.pcm 与 .crc 记录是否一致(断电文件损坏排查)"""
import glob
import os
import zlib

for f in sorted(glob.glob("/root/pa-files/*.pcm")):
    data = open(f, "rb").read()
    crc = "%08x" % (zlib.crc32(data) & 0xffffffff)
    side = f + ".crc"
    want = open(side).read().strip() if os.path.exists(side) else "(无)"
    ok = "OK" if crc == want else "损坏!"
    print(os.path.basename(f), len(data), "字节 实际=", crc, "记录=", want, ok)
