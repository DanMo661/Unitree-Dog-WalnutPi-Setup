# -*- coding: utf-8 -*-
"""test_release_files.py —— 发布组装「部署集差集校验」验证(make_release.validate_snapshot)

背景: 手写 ROOT_FILES 清单历史上三次漏文件(netrc.py/languard.py 同款坑,
5a3c909 为最新一次), make_release.py 现在拿 deploy_pc.collect_files(真身)
与快照做双向差集:
  部署集有、快照没有 -> 硬错误 sys.exit(1)(红用例复现该事故类)
  快照有、部署集没有 -> 仅 WARN(测试/APK 等预期多出, 不挡发布)
用法: python run_test_shim.py test_release_files.py
"""
import importlib.util
import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))

# 按路径加载 tools/make_release.py(main() 有 __main__ 守卫, 加载无副作用; 集成用例经
# load_deploy_set 间接触及 deploy_pc 的模块级 paramiko, 缺失时按 SystemExit 记 FAIL 提示安装)
_spec = importlib.util.spec_from_file_location(
    "_make_release_under_test", os.path.join(HERE, "tools", "make_release.py"))
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)

FAILS = []


def ok(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


FULL = ["bridge.py", "config.py", "languard.py", "netrc.py",
        "core/__init__.py", "core/fsm.py", "static/index.html"]


def main():
    # 绿: 部署集与快照完全一致 -> 缺失清单为空
    ok(mr.validate_snapshot(set(FULL), list(FULL)) == [],
       "绿: 部署集与快照完全一致 -> 缺失清单为空")

    # 绿: Path 与 Windows 反斜杠混传, 归一化后等价
    ok(mr.validate_snapshot(set(FULL),
                            [Path(p.replace("/", os.sep)) for p in FULL]) == [],
       "绿: Path/反斜杠与 POSIX 字符串归一后等价")

    # 红: 快照侧抽掉 languard.py(复现 5a3c909 事故) -> 必须进缺失清单
    miss = mr.validate_snapshot(set(FULL), [p for p in FULL if p != "languard.py"])
    ok(miss == ["languard.py"], "红: 快照缺 languard.py -> 缺失清单恰为 ['languard.py']")

    # 红: 缺多个文件时逐个列出
    miss2 = mr.validate_snapshot(set(FULL), ["bridge.py"])
    ok(miss2 == sorted(set(FULL) - {"bridge.py"}),
       "红: 快照只留 bridge.py -> 其余 %d 个全部进缺失清单" % (len(FULL) - 1))

    # 反方向: 快照多出(APK/安装脚本等部署集没有)不算缺失 -> 不应触发硬错误
    ok(mr.validate_snapshot(set(FULL),
                            FULL + ["PaSend-v36-网络遥控.apk", "install.sh"]) == [],
       "绿(反方向): 快照多出 APK/安装脚本不进缺失清单(仅 WARN)")

    # 集成: 真身实测 deploy_pc.collect_files 与当前 make_release 采集集零缺失
    try:
        deploy = mr.load_deploy_set(Path(HERE))
        ok("languard.py" in deploy, "集成: 真身 collect_files 含 languard.py(事故回归哨兵)")
        ok("static/index.html" in deploy, "集成: collect_files 含 static 页面")
        ok(mr.validate_snapshot(deploy, mr.collect(Path(HERE))) == [],
           "集成: 真身部署集 vs make_release.collect 快照零缺失(当前仓状态)")
    except SystemExit as e:
        ok(False, "集成: 加载 deploy_pc.collect_files 失败(缺 paramiko?): %s" % e)

    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), FAILS))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
