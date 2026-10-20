# -*- coding: utf-8 -*-
"""journal.py 黑匣子加固自测(本地 tempfile 目录, 不碰板端路径):
a) 关键记录(event / 非 Move cmd)写后 fsync 且文件内容完整可解析; Move/frame/link 不 fsync
b) init → close(clean=True) → 再 init 判 'clean'
c) 不 close 直接再 init 判 'unclean'; close(clean=False) 后再 init 同样判 'unclean'
d) tail(n) 行数/顺序/钳制
e) 超过 max_mb 仍正常轮转到下一序号文件, init 续接最后一个文件
用法: python run_test_shim.py test_journal.py
"""
import json
import os
import shutil
import sys
import tempfile

import journal

FAILS = []
DIRS = []  # 建过的临时目录, 结束统一清


def ok(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


def tmpdir(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    DIRS.append(d)
    return d


def read_lines(d, seq):
    """读指定序号文件的全部行; 文件不存在返回 None。"""
    p = os.path.join(d, "journal-%04d.jsonl" % seq)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return f.read().splitlines()


def parse_all(lines):
    """逐行 json.loads, 全部成功返回对象列表, 否则 None。"""
    out = []
    for ln in lines or []:
        try:
            out.append(json.loads(ln))
        except Exception:
            return None
    return out


def main():
    try:
        run()
    finally:
        journal.close()
        for d in DIRS:
            shutil.rmtree(d, ignore_errors=True)
    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), FAILS))
        sys.exit(1)
    print("全部通过")


def run():
    print("== a. 关键记录 fsync + 内容完整 ==")
    d = tmpdir("journal_test_")
    ok(journal.init(d, max_mb=32), "init 成功")
    ok(journal.last_stop() == "unknown", "全新目录判 unknown (%r)" % journal.last_stop())

    # 包一层计数器再调真 fsync, 直接验证哪些 kind 走了 fsync
    calls = {"n": 0}
    real_fsync = os.fsync

    def counting_fsync(fd):
        calls["n"] += 1
        real_fsync(fd)

    journal.os.fsync = counting_fsync
    try:
        journal.record("event", ev="arm", src="rc")
        ok(calls["n"] == 1, "event 记录触发 fsync (n=%d)" % calls["n"])
        journal.record("cmd", name="BalanceCtrl", p=1)
        ok(calls["n"] == 2, "非 Move cmd 触发 fsync (n=%d)" % calls["n"])
        journal.record("cmd", name="Move", x=1.0, y=0.0)
        ok(calls["n"] == 2, "Move cmd 只 flush 不 fsync (n=%d)" % calls["n"])
        journal.record("frame", ch=[1, 2, 3])
        ok(calls["n"] == 2, "frame 只 flush 不 fsync (n=%d)" % calls["n"])
        journal.record("link", link="up")
        ok(calls["n"] == 2, "link 只 flush 不 fsync (n=%d)" % calls["n"])
    finally:
        journal.os.fsync = real_fsync

    # 不 close, 直接开新句柄读盘上文件(模拟 fsync 后断电前内容已在盘上)
    lines = read_lines(d, 1)
    ok(lines is not None and len(lines) == 5,
       "5 条全部在盘上, 无半行丢失 (%s)" % (len(lines) if lines else "文件不存在"))
    objs = parse_all(lines)
    ok(objs is not None and [o["kind"] for o in objs] == ["event", "cmd", "cmd", "frame", "link"],
       "每行都是完整 JSON 且顺序正确")
    ok(objs is not None and objs[0].get("ev") == "arm" and objs[2].get("name") == "Move",
       "payload 字段原样保留")

    print("== b. 干净停机 → clean ==")
    journal.close(clean=True)
    ok(os.path.exists(os.path.join(d, ".clean")), "close(clean=True) 写 .clean")
    ok(not os.path.exists(os.path.join(d, ".running")), "close(clean=True) 删 .running")
    ok(journal.init(d), "再 init")
    ok(journal.last_stop() == "clean", "判 clean (%r)" % journal.last_stop())
    ok(os.path.exists(os.path.join(d, ".running")), "init 写 .running")
    ok(not os.path.exists(os.path.join(d, ".clean")), "init 删 .clean")
    with open(os.path.join(d, ".running"), "r", encoding="utf-8") as f:
        mk = f.read()
    ok("pid=%d" % os.getpid() in mk, ".running 含本进程 pid")
    ok("time=" in mk, ".running 含时间")

    print("== c. 异常停机 → unclean ==")
    journal.record("event", ev="crash_sim")
    ok(journal.init(d), "不 close 直接再 init")
    ok(journal.last_stop() == "unclean", "判 unclean (%r)" % journal.last_stop())
    journal.close(clean=False)
    ok(os.path.exists(os.path.join(d, ".running")), "close(clean=False) 保留 .running")
    ok(not os.path.exists(os.path.join(d, ".clean")), "close(clean=False) 不写 .clean")
    ok(journal.init(d), "close(clean=False) 后再 init")
    ok(journal.last_stop() == "unclean", "close(clean=False) 后判 unclean (%r)" % journal.last_stop())

    print("== d. tail(n) ==")
    journal.record("event", ev="t1")
    journal.record("event", ev="t2")
    journal.record("event", ev="t3")
    full = read_lines(d, 1)
    ok(full and len(full) >= 3, "文件持续追加 (%d 行)" % len(full))
    t3 = journal.tail(3)
    ok(t3 == full[-3:], "tail(3) 是最后 3 行且顺序与文件一致")
    ok(json.loads(t3[-1])["ev"] == "t3", "最后一行是最后写入的 t3")
    ok(journal.tail(1) == [full[-1]], "tail(1) 取末行")
    ok(journal.tail(0) == [full[-1]], "tail(0) 钳制为 1 行")
    ok(journal.tail(100000) == full, "超大 n 钳制(上限 500, 共 %d 行全给)" % len(full))
    journal.close()
    ok(journal.tail(5) == [], "close 后 tail 返回 []")
    try:
        journal.record("event", ev="silent")
        silent = True
    except Exception:
        silent = False
    ok(silent, "未 init 的 record 静默返回不上抛")

    print("== e. 轮转仍然正常 ==")
    d2 = tmpdir("journal_rot_")
    ok(journal.init(d2, max_mb=1), "init max_mb=1")
    for i in range(700):  # 每条约 2KB → 总量约 1.4MB, 必然跨过 1MB 触发轮转
        journal.record("frame", pad="x" * 2000, i=i)
    l1, l2, l3 = read_lines(d2, 1), read_lines(d2, 2), read_lines(d2, 3)
    ok(l1 and l2 and l3 is None, "已轮转: 0001/0002 存在, 无 0003")
    ok(l1 and json.loads(l1[0])["i"] == 0 and l2 and json.loads(l2[-1])["i"] == 699,
       "0001 从 i=0 开始, 0002 收尾 i=699, 中间不丢条")
    ok(len(l1) + len(l2) == 700, "700 条一条不少 (%d+%d)" % (len(l1), len(l2)))
    ok(journal.tail(1) == [l2[-1]], "tail 读的是当前文件(0002)")
    journal.close(clean=True)
    ok(journal.init(d2), "轮转后 init")
    ok(journal.last_stop() == "clean", "轮转目录判 clean")
    ok(journal.tail(1) == [l2[-1]], "init 续接最后一个文件(0002) 而非新开")


if __name__ == "__main__":
    main()
