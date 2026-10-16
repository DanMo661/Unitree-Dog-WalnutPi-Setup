#!/bin/bash
# 在板子上执行: bash install.sh
# apt 部分需要 sudo 通行证; 若刚才已用 sudo -S 装过同样的包, 这里的 -n 会直接跳过
set -e
cd "$(dirname "$0")"

sudo -n apt-get update -y 2>/dev/null || true
sudo -n apt-get install -y python3-venv python3-dev portaudio19-dev libportaudio2 \
     build-essential libgl1 libglib2.0-0 2>/dev/null || \
     echo "提示: apt 未执行(无 sudo 通行证), 若下面 venv/编译报错先手动补装上述包"

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -U pip wheel -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

.venv/bin/python test_sbus.py
echo
echo "安装完成。接下来按 README.md 五步走: 1)sbus_monitor 实测通道 2)验证连狗 3)联调 4)失控测试 5)自启"
