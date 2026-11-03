# -*- coding: utf-8 -*-
"""test_entry_alias.py -- 入口双实例守门(2026-11-02 现场事故回归)。

python bridge.py 运行时模块名是 __main__，link.py/webserver.py 的惰性 `import bridge`
会加载第二副本(全 None)。入口守卫必须把真身别名注册进 sys.modules，否则:
  web arm 的 FSM 半段被跳过(web_armed 250ms 内被镜像刷回 False)、axes 死路、
  DogLink 重连回调 reset 打到假副本。
webface 测试以模块方式 import bridge，复现不了此 bug -- 只能锁源码形态。
用法: python run_test_shim.py test_entry_alias.py
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FAILS = []


def ok(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


def read(name):
    with io.open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def main():
    b = read("bridge.py")
    # 1) 入口守卫里必须注册 bridge 别名(容忍单双引号)
    idx = b.find("__main__")
    guard = b[b.rfind("if __name__"):]
    ok('sys.modules["bridge"]' in guard.replace("'", '"'),
       "入口 __main__ 守卫注册 sys.modules['bridge'] 别名")

    # 2) 别名必须在 main() 调用之前(线程起后就来不及了)
    ok(guard.index("sys.modules") < guard.index("main()"),
       "别名注册先于 main()(线程尚未启动)")

    # 3) 两个惰性 import 点仍在(别名一旦删掉, 它们就是哑弹, 此测试必须红)
    ok("import bridge" in read("link.py"), "link.py 惰性 import bridge 在位")
    ok("import bridge" in read("webserver.py"), "webserver.py 惰性 import bridge 在位")

    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), FAILS))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
