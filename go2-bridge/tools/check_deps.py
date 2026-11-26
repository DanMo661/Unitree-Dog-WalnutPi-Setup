# -*- coding: utf-8 -*-
"""依赖方向守门: 用 ast 静态检查 import, 不执行任何被检代码。

规则(2026-10-31 核实现状后落地的两条边界):
  R1  core/ 自包含: core/ 下所有 .py 只准 import stdlib、core 包自身(相对导入或
      import core.*)与豁免根级模块 config —— config 是纯常量模块(仅 import sys +
      可选 config_local), fsm 构造参数可注入同名属性对象规避; core/fsm.py:51
      import config 是今日唯一既有根级依赖, 属设计内豁免。import 其他任何顶层名
      (bridge/sbus/pa/journal 等根级模块、第三方包、无法归类的名字)一律违规。
      理由: core 必须能脱离主程序独立测试。
  R2  languard 纯 stdlib: languard.py 只准 import stdlib 顶层名; 相对导入与
      项目/第三方模块一律违规。理由: 守卫模块要能被任何测试零依赖复用。

用法: python tools/check_deps.py [--root <go2-bridge目录>]
  缺省 root = 本脚本所在目录的上级(即 go2-bridge)。
  退出码: 0=全过(含 SKIP) / 1=有违规 / 2=--root 或版本参数错误。
  规则目标缺失(core/ 或 languard.py 不存在)按 SKIP(WARN) 处理, 不算失败,
  但摘要行会点名 SKIP 数——全 SKIP 的"全过"不是全量绿, CI 上别当通过看。
  被扫文件语法错误/解不开码(如 GBK 声明)按违规报, 目录伪装 .py 跳过, 均不崩溃。
  需 Python 3.10+(sys.stdlib_module_names, main 有运行时护栏)。静态分析盲区:
  __import__/exec/importlib 等动态导入看不见, 靠 review 兜住(本仓已 grep 确认零使用)。
  TYPE_CHECKING 块内的违规 import 也计违规——有意 fail-closed, 仅注解用途请改字符串注解。
"""
import argparse
import ast
import sys
from pathlib import Path

# R1 豁免白名单: core 允许依赖的根级模块(新增豁免必须在此登记并写明理由)。
# 匹配精确小写 "config": `import Config` 在 Windows 检查机上会真加载同一文件,
# 但 Linux 板(靶机)上它本就是 ModuleNotFoundError 死代码, 判违规是正确口径。
CORE_ROOT_ALLOW = {"config"}


def _top(name):
    """取 import 顶层名: "a.b.c" -> "a" """
    return (name or "").split(".")[0]


def _iter_imports(tree):
    """遍历 AST, yield (行号, 描述, 顶层名, 相对层级); 覆盖 import a.b / from a.b import c"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield node.lineno, "import " + a.name, _top(a.name), 0
        elif isinstance(node, ast.ImportFrom):
            mod = "." * node.level + (node.module or "")
            names = ",".join(a.name for a in node.names)
            yield node.lineno, "from %s import %s" % (mod, names), _top(node.module), node.level


def _load(path, root, rule):
    """解析单文件 -> (import 列表, None) 或 (None, 语法错误明细行)"""
    rel = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except SyntaxError as e:
        return None, "  %s:%s: 语法错误(无法验证) —— 违反 %s" % (rel, e.lineno or 0, rule)
    except ValueError as e:     # 含 UnicodeDecodeError(如 GBK 声明文件): 解不开=无法验证=违规
        return None, "  %s:0: 解码失败(无法验证: %s) —— 违反 %s" % (rel, type(e).__name__, rule)
    return list(_iter_imports(tree)), None


def check_r1(root):
    """R1: core/ 只准依赖 stdlib + core 自身 + 豁免 config。-> (状态, 一行标题, 明细, 违规数)"""
    core = root / "core"
    line = "[R1] core 自包含(只准 stdlib+core自身+豁免:%s)" % (
        ",".join(sorted(CORE_ROOT_ALLOW)) or "无")
    if not core.is_dir():
        return "SKIP", line, ["  WARN: 未找到 core/, 规则目标不存在, 跳过"], 0
    bad, nf = [], 0
    for f in sorted(core.rglob("*.py")):
        if not f.is_file():     # 目录伪装 .py(如 core/fake.py/)跳过, 不让 read_text 崩
            continue
        nf += 1
        imps, err = _load(f, root, "R1")
        if err:
            bad.append(err)
            continue
        rel = f.relative_to(root).as_posix()
        for ln, desc, top, level in imps:
            if level == 1:                              # 包内相对导入 = core 自身, 放行
                continue
            if level > 1:                               # .. 越出 core 包 = 走私通道, 违规
                bad.append("  %s:%d: %s —— 违反 R1(相对导入越出 core 包)" % (rel, ln, desc))
                continue
            if top == "core" or top in sys.stdlib_module_names or top in CORE_ROOT_ALLOW:
                continue
            bad.append("  %s:%d: %s —— 违反 R1(core 禁止 import 根级模块/第三方包)"
                       % (rel, ln, desc))
    if not nf:
        return "SKIP", line, ["  WARN: core/ 下没有 .py 文件, 规则目标不存在, 跳过"], 0
    line += ", 扫 %d 文件" % nf
    return ("FAIL" if bad else "PASS"), line, bad, len(bad)


def check_r2(root):
    """R2: languard.py 只准依赖 stdlib。-> (状态, 一行标题, 明细, 违规数)"""
    f = root / "languard.py"
    line = "[R2] languard 纯 stdlib"
    if not f.is_file():
        return "SKIP", line, ["  WARN: 未找到 languard.py, 规则目标不存在, 跳过"], 0
    imps, err = _load(f, root, "R2")
    if err:
        return "FAIL", line + ", 扫 1 文件", [err], 1
    bad = []
    for ln, desc, top, level in imps:
        if level or top not in sys.stdlib_module_names:  # 相对导入/项目/第三方 都违规
            bad.append("  languard.py:%d: %s —— 违反 R2(languard 只准 import stdlib)"
                       % (ln, desc))
    return ("FAIL" if bad else "PASS"), line + ", 扫 1 文件", bad, len(bad)


def run_checks(root):
    """跑全部规则 -> (退出码, 输出行列表, 违规总数, SKIP 规则数)"""
    root = Path(root)
    out, nbad, nskip = [], 0, 0
    for status, line, details, n in (check_r1(root), check_r2(root)):
        out.append("%s: %s" % (line, status))
        out += details
        nbad += n
        nskip += status == "SKIP"
    return (1 if nbad else 0), out, nbad, nskip


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="go2-bridge 依赖方向守门(静态 ast, 不执行被检代码)")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                    help="go2-bridge 目录(缺省=本脚本所在目录的上级)")
    a = ap.parse_args(argv)
    if sys.version_info < (3, 10):
        print("check_deps: 需 Python 3.10+(sys.stdlib_module_names), 当前 %d.%d"
              % sys.version_info[:2], file=sys.stderr)
        return 2
    root = Path(a.root)
    if not root.is_dir():
        print("check_deps: --root 不是目录: %s" % a.root, file=sys.stderr)
        return 2
    code, out, nbad, nskip = run_checks(root.resolve())
    print("\n".join(out))
    if nbad:
        print("check_deps: 违规 %d 处" % nbad)
    else:
        print("check_deps: 全过"
              + ("（含 SKIP %d 项: 规则目标缺失, 非全量绿）" % nskip if nskip else ""))
    return code


if __name__ == "__main__":
    sys.exit(main())
