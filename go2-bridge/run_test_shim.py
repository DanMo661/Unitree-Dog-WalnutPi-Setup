# -*- coding: utf-8 -*-
"""本地(无 audioop 的 py3.13+)跑板端测试的桩包装: python run_test_shim.py test_xxx.py"""
import array
import runpy
import sys
import types


def _mul(b, w, g):
    a = array.array("h")
    a.frombytes(b)
    return array.array("h", (int(v * g) for v in a)).tobytes()


def _max(b, w):
    a = array.array("h")
    a.frombytes(b)
    return max((abs(v) for v in a), default=0)


m = types.ModuleType("audioop")
m.mul, m.max = _mul, _max
sys.modules["audioop"] = m
runpy.run_path(sys.argv[1], run_name="__main__")
