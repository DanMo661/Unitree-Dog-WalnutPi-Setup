#!/usr/bin/env python3
"""从真身仓 (go2-bridge) 组装公开发布目录。

只拷贝运行集文件 + 写入 VERSION（真身仓 git HEAD 短哈希）。
本脚本不做任何 git add/commit/push。

用法:
    python tools/make_release.py <输出目录> [--src <真身go2-bridge路径>]
"""

import argparse
import fnmatch
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
    "PaSend-v36-网络遥控.apk",  # deploy_page.py 上架的成品(缺它发布树无法直接上架)
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


def git_short_head(src: Path) -> str:
    r = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if r.returncode != 0:
        sys.exit(f"git rev-parse 失败: {r.stderr.strip()}")
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
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
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


if __name__ == "__main__":
    main()
