# -*- coding: utf-8 -*-
"""test_deps.py —— 依赖方向守门回归(tools/check_deps.py, 2026-10-31)

守三件事:
  1. 真身当前全绿: 两规则 R1(core 自包含)/R2(languard 纯 stdlib) 必须 PASS
     (非 SKIP)、CLI 退出码 0 —— 谁往 core/ 加了根级依赖、或给 languard 引入
     非stdlib 包, 这条当场红。
  2. 守门器本身抓得住违规: temp 里搭违规沙箱(core import bridge / languard
     import journal / 语法错误文件)必须 FAIL 且逐处点名; 洗白后必须回绿。
  3. 健壮性契约: 规则目标缺失 -> SKIP(WARN) 不算失败; --root 不存在 -> 退出码 2。

用法: python run_test_shim.py test_deps.py   (路径从 __file__ 推, 不依赖 cwd;
      本测试只用 stdlib, 不碰硬件/网络, 沙箱建在 temp 不清理)
"""
import importlib.util
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKER = os.path.join(HERE, "tools", "check_deps.py")

FAILS = []


def ok(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


def _load_checker():
    """按文件位置加载 tools/check_deps.py(tools 非包, 不能 import)"""
    spec = importlib.util.spec_from_file_location("check_deps", CHECKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _w(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_real_root_clean(cd):
    """真身全绿: 两规则 PASS + CLI 退出码 0"""
    code, out, nbad, _ = cd.run_checks(HERE)
    ok(code == 0 and nbad == 0, "真身 run_checks 退出码 0 且零违规")
    ok(any(l.startswith("[R1]") and l.endswith("PASS") for l in out),
       "R1 在真身上 PASS(非 SKIP)")
    ok(any(l.startswith("[R2]") and l.endswith("PASS") for l in out),
       "R2 在真身上 PASS(非 SKIP)")
    r = subprocess.run([sys.executable, CHECKER, "--root", HERE],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok(r.returncode == 0, "CLI --root 真身 退出码 0")


def test_red_sandbox(cd):
    """违规沙箱: core import bridge + languard import journal + 语法错误 必须逐处点名(红)"""
    sb = tempfile.mkdtemp(prefix="checkdeps_red_")
    os.makedirs(os.path.join(sb, "core"))
    _w(os.path.join(sb, "core", "fsm.py"), "import bridge\n")          # 违反 R1
    _w(os.path.join(sb, "core", "broken.py"), "def f(:\n")             # 语法错误=违规
    _w(os.path.join(sb, "languard.py"), "import journal\n")            # 违反 R2
    code, out, nbad, _ = cd.run_checks(sb)
    text = "\n".join(out)
    ok(code == 1 and nbad == 3, "违规沙箱退出码 1 且数出 3 处违规")
    ok("core/fsm.py" in text and "import bridge" in text, "R1 点名 core/fsm.py: import bridge")
    ok("languard.py" in text and "import journal" in text, "R2 点名 languard.py: import journal")
    ok("broken.py" in text and "语法错误" in text, "语法错误文件按违规报(不崩溃)")
    # 洗白 -> 绿
    _w(os.path.join(sb, "core", "fsm.py"), "import math\n")
    _w(os.path.join(sb, "languard.py"), "import hmac\n")
    os.remove(os.path.join(sb, "core", "broken.py"))
    code, out, nbad, _ = cd.run_checks(sb)
    ok(code == 0 and nbad == 0, "沙箱洗白后退出码 0(绿)")
    print("  (沙箱留在 temp 不清理: %s)" % sb)


def test_hardening(cd):
    """nitpick-③ 返修回归: GBK 文件/目录伪装 .py/越包相对导入 都要按规矩报"""
    sb = tempfile.mkdtemp(prefix="checkdeps_hard_")
    os.makedirs(os.path.join(sb, "core"))
    gbk = os.path.join(sb, "core", "gbk.py")
    with open(gbk, "wb") as f:
        f.write("# -*- coding: gbk -*-\nx = '中文'\n".encode("gbk"))
    os.makedirs(os.path.join(sb, "core", "fake.py"))                      # 目录伪装 .py
    _w(os.path.join(sb, "core", "esc.py"), "from ..bridge import Dog\n")  # level=2 越包
    _w(os.path.join(sb, "languard.py"), "import hmac\n")
    code, out, nbad, _ = cd.run_checks(sb)
    text = "\n".join(out)
    ok(code == 1 and nbad == 2, "加固沙箱退出码 1 且数出 2 处违规(GBK 文件+越包相对导入)")
    ok("gbk.py" in text and "解码失败" in text, "GBK 声明文件按违规报(不黑屏)")
    ok("esc.py" in text and "越出 core 包" in text, "from ..bridge(level=2) 按走私报")
    ok("fake.py" not in text, "目录伪装 .py 被静默跳过(零 traceback 零点名)")
    _w(gbk, "x = 'ok'\n")                                                 # 洗白
    os.remove(os.path.join(sb, "core", "esc.py"))
    code, out, nbad, _ = cd.run_checks(sb)
    ok(code == 0 and nbad == 0, "加固沙箱洗白后回绿")
    print("  (沙箱留在 temp 不清理: %s)" % sb)


def test_skip_and_badroot(cd):
    """目标缺失 -> SKIP 不算失败; --root 不存在 -> 退出码 2"""
    sb = tempfile.mkdtemp(prefix="checkdeps_skip_")
    code, out, nbad, _ = cd.run_checks(sb)
    ok(code == 0 and sum(": SKIP" in l for l in out) == 2,
       "空 root 两规则 SKIP(WARN) 且退出码 0")
    r = subprocess.run([sys.executable, CHECKER, "--root", sb],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok(r.returncode == 0 and "SKIP 2 项" in r.stdout, "全 SKIP 时摘要点名 SKIP 数(防假绿)")
    r = subprocess.run([sys.executable, CHECKER, "--root",
                        os.path.join(sb, "no_such")], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    ok(r.returncode == 2, "CLI --root 不存在 退出码 2(清晰报错)")


def main():
    cd = _load_checker()
    test_real_root_clean(cd)
    test_red_sandbox(cd)
    test_hardening(cd)
    test_skip_and_badroot(cd)
    if FAILS:
        print("test_deps: %d 项失败: %s" % (len(FAILS), "; ".join(FAILS)))
        raise SystemExit(1)
    print("test_deps: 全部通过")


if __name__ == "__main__":
    main()
