#!/usr/bin/env python3
"""从真身仓 (go2-bridge) 组装公开发布目录。

只拷贝运行集文件 + 写入 VERSION（真身仓 git HEAD 短哈希）。
本脚本不做任何 git add/commit/push。

用法:
    python tools/make_release.py <输出目录> [--src <真身go2-bridge路径>]
"""

import argparse
import fnmatch
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_SRC = Path(r"D:/Work_Project/ZCodeProject/OrangePi-Zero2W/go2-bridge")

# 固定文件清单：存在者拷贝，缺失者跳过并提示
ROOT_FILES = [
    "bridge.py",
    "config.py",
    "sbus.py",
    "servo.py",
    "pa.py",
    "pa_recv.py",
    "netrc.py",
    "journal.py",
    "languard.py",   # 10-31 补: 手写清单第三次漏文件(netrc.py/languard.py 同款坑), 见 git log
    "stats.py",      # 10-31 bridge 拆分: SbusStats 帧统计(bridge re-export, deploy 集自动发现)
    "telemetry.py",  # 10-31 bridge 拆分: State/log/事件环(bridge re-export, deploy 集自动发现)
    "link.py",       # 10-31 bridge 拆分: DogLink/WebRTC 链路(bridge re-export, deploy 集自动发现)
    "webserver.py",  # 10-31 bridge 拆分: FastAPI 网页服务(bridge re-export, deploy 集自动发现)
    "selfcheck.py",  # 11-04 开机自检(第五次手写清单漏文件: gas.py 同日双响)
    "gas.py",        # 11-03 气体传感器读取(dog4 加装; nitpick 抓的第四次手写清单漏文件)
    "go2-bridge.service",
    "go2-audio.service",
    "install.sh",
    "requirements.txt",
    "run_test_shim.py",
    "deploy_pc.py",
    "deploy_page.py",
    "board_wifi_setup.py",
    "push_song.py",     # 预设音频上载工具(a172116 起随运行集同步)
    "sbus_monitor.py",  # SBUS 实时帧流(换遥控器重标定用, SOP §5 引用)
    "README.md",        # 11-03 补: 使用者文档, 公开仓 face(清空输出目录时曾被扫掉)
    "calibrate.py",     # 11-03 补: f06f7cc 白名单决策, 10-31 清单重构时遗漏
    "PaSend-v49-网络遥控.apk",  # 上一版(保留可回装)
    "PaSend-v50-网络遥控.apk",  # 上一版(保留可回装)
    "PaSend-v51-网络遥控.apk",  # 上一版(保留可回装)
    "PaSend-v52-网络遥控.apk",  # 上一版(保留可回装)
    "PaSend-v53-网络遥控.apk",  # 上一版(保留可回装)
    "PaSend-v54-网络遥控.apk",  # 上一版(保留可回装)
    "PaSend-v55-网络遥控.apk",  # deploy_page.py 上架的成品(v55: STAT? 播放态对账+喊话前显式停播)
    "PaSend-v36-网络遥控.apk",  # 11-03 补: v36 装机锚点(board-good-20261030-1), 永不覆盖
]

# 根目录通配文件
ROOT_GLOBS = ["setup*.py", "test_*.py"]

# 目录集：core/ 只拷 .py 源码；static/ 只拷 .html
PY_SOURCE_DIRS = ["core"]
HTML_DIRS = ["static"]

# 明确排除（按路径每一段匹配，wheels*/sbus_diag* 为前缀通配）
EXCLUDE = [
    "CLAUDE.md",
    "TODO.md",
    "ARCHITECTURE.md",
    "OPTIMIZATION.md",
    "config_local.py",
    "journals",
    "__pycache__",
    "wheels*",
    "sbus_diag*",
    "archive",
    "research",
    "android-app",
    # 带凭据/已归档的板级脚本绝不进公开仓(历史教训: 6075fb6 删除, 6de8db7 脱敏)
    "setup_dog2.py",             # 内含 WiFi SSID/PSK 明文(dog2 专属, 复核 HIGH 定案)
    "setup2_serial_netplan.py",  # 香橙派旧脚本, 公开仓 30436ff 已归档 legacy/
    # 开发仓结构审计测试: 查 tools/ 下脚本, 公开仓不带 tools/ 必假红(11-03 发布门实测)
    "test_deps.py",
    "test_release_files.py",
]


def is_excluded(rel: Path) -> bool:
    return any(fnmatch.fnmatch(part, pat) for part in rel.parts for pat in EXCLUDE)


def collect(src: Path) -> list[Path]:
    """返回待拷贝的相对路径列表（已去重、已排除）。"""
    rels: list[Path] = []
    for name in ROOT_FILES:
        if (src / name).is_file():
            rels.append(Path(name))
        else:
            print(f"[skip] 真身缺少文件: {name}")
    for pat in ROOT_GLOBS:
        rels += [p.relative_to(src) for p in sorted(src.glob(pat)) if p.is_file()]
    for d in PY_SOURCE_DIRS:
        rels += [p.relative_to(src) for p in sorted((src / d).rglob("*.py")) if p.is_file()]
    for d in HTML_DIRS:
        rels += [p.relative_to(src) for p in sorted((src / d).glob("*.html")) if p.is_file()]

    seen: set[str] = set()
    out: list[Path] = []
    for rel in sorted(set(rels)):
        if is_excluded(rel):
            print(f"[excl] 命中排除列表: {rel.as_posix()}")
            continue
        out.append(rel)
    return out


def load_deploy_set(src: Path) -> set[str]:
    """按路径加载 deploy_pc.py, 调 collect_files(真身) 取部署运行集(POSIX 相对路径集合)。

    不走普通 import: tools/ 不在 sys.path 上, 且按路径加载不污染 sys.modules。
    deploy_pc.py 模块级只做 stdlib/paramiko 导入和环境变量读取, main() 有 __main__
    守卫, 按路径 exec 无副作用; 运行环境缺 paramiko 会在此失败并给出可操作提示。
    """
    dp = src / "deploy_pc.py"
    if not dp.is_file():
        sys.exit(f"找不到 {dp}, 部署集差集校验失去基准(该文件枚举真正要部署的运行集)")
    spec = importlib.util.spec_from_file_location("_release_deploy_pc", dp)
    if spec is None or spec.loader is None:
        sys.exit(f"无法为 {dp} 构造 import spec")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ImportError as e:
        sys.exit(f"加载 deploy_pc.py 失败: {e}\n"
                 "校验依赖其模块级导入(通常缺 paramiko): 先 pip install paramiko 再重跑。")
    # 按 EXCLUDE 过滤: collect_files 会把真身存在的 config_local.py 计入部署集,
    # 而公开发布树有意排除它(板级私有覆盖, 可含 WiFi PSK)——不过滤就会假 FAIL
    return {d for d in (str(p).replace("\\", "/") for p in mod.collect_files(str(src)))
            if not is_excluded(Path(d))}


def validate_snapshot(deploy_set, snapshot_rels) -> list[str]:
    """部署集差集校验: 返回「部署集有、快照没有」的缺失清单(硬错误项, 调用方 sys.exit(1))。

    反方向(快照有、部署集没有: 测试/APK/安装脚本等预期多出)不算缺失, 由调用方打 WARN。
    两边都归一化成 POSIX 风格相对路径再比, 兼容 Path 与 str 混传。
    """
    snap = {str(r).replace("\\", "/") for r in snapshot_rels}
    return sorted(str(d).replace("\\", "/") for d in deploy_set
                  if str(d).replace("\\", "/") not in snap)


def git_short_head(src: Path) -> str:
    r = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if r.returncode != 0:
        sys.exit(f"git rev-parse 失败({src}): {r.stderr.strip()}\n"
                 "VERSION 取自 git HEAD —— --src 必须指向 git 仓内的真身目录。")
    return r.stdout.strip()


def main() -> None:
    ap = argparse.ArgumentParser(description="组装 go2-bridge 公开发布目录")
    ap.add_argument("out", type=Path, help="输出目录（发布树根）")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC, help="真身 go2-bridge 路径")
    args = ap.parse_args()

    src = args.src.resolve()
    if not src.is_dir():
        sys.exit(f"源目录不存在: {src}")

    rels = collect(src)

    # 部署集差集校验(2026-10-31): 手写 ROOT_FILES 已三次漏文件(netrc.py/languard.py
    # 同款坑, 最新 5a3c909), 拿 deploy_pc.collect_files(真身) 的运行集与快照做双向差集兜底。
    # 校验只依赖 src, 放在拷贝之前——失败时不落半成品树(带合法 VERSION 的半成品会被
    # deploy_pc 当可用发布树误部署)。
    deploy_set = load_deploy_set(src)
    if not deploy_set or "bridge.py" not in deploy_set:
        sys.exit("[FAIL] 部署集基准失效: collect_files 返回 %d 个文件%s —— 入口脚本改名或"
                 "枚举逻辑坏了, 校验失去基准, 先修 deploy_pc.collect_files 再组装"
                 % (len(deploy_set), "(且无 bridge.py)" if deploy_set else ""))
    missing = validate_snapshot(deploy_set, rels)
    extras = sorted({str(r).replace("\\", "/") for r in rels} - deploy_set)
    if missing:
        print("[FAIL] 公开发布树缺运行文件(历史事故类: 5a3c909 languard 老版进公开仓), 逐个如下:")
        for m in missing:
            print(f"  - {m}")
        print("修复方向二选一:")
        print("  1) 真身新增了运行文件 -> 补进本脚本 ROOT_FILES(或对应目录集);")
        print("  2) deploy_pc.collect_files 枚举范围变了(如换入口脚本/加目录) -> 两边同步后重跑。")
        print("  特例: config_local.py 是板级私有覆盖(可含 WiFi PSK), EXCLUDE 有意排除它——")
        print("  缺失清单里出现它属预期, 千万不要照方向 1 补进 ROOT_FILES(泄密事故)。")
        sys.exit(1)
    for e in extras:
        print(f"[WARN] 快照有、部署集没有(测试/安装脚本/服务单元/APK 等发布树预期多出, 不挡发布): {e}")

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    rel_posix = {r.as_posix() for r in rels}
    stale = sorted(p.relative_to(out).as_posix() for p in out.rglob("*")
                   if p.is_file() and p != out / "VERSION"
                   and p.relative_to(out).as_posix() not in rel_posix)
    if stale:
        print("[FAIL] 输出目录含本次组装不会写入的陈旧文件(上次组装残留, 源里已删/改名), 逐个如下:")
        for s in stale:
            print(f"  - {s}")
        print("清空输出目录或换新目录后重跑; 带残留发布 = 旧版文件进公开仓(5a3c909 镜像事故)。")
        sys.exit(1)

    for rel in rels:
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, dst)

    version = git_short_head(src)
    (out / "VERSION").write_text(version, encoding="ascii")

    print(f"拷贝 {len(rels)} 个文件 -> {out}")
    for rel in rels:
        print(f"  {rel.as_posix()}")
    print(f"VERSION = {version}")
    print(f"[校验] 部署集差集 PASS: 缺失 0, 部署集 {len(deploy_set)} 个文件全部在快照中; "
          f"快照多出 {len(extras)} 个(见上 WARN)")


if __name__ == "__main__":
    main()
