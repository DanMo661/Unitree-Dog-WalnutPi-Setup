#!/bin/bash
# 在开发板上执行:  bash install.sh
set -e
sudo apt update
sudo apt install -y python3-pip portaudio19-dev
pip3 install -r "$(dirname "$0")/requirements.txt"
echo
echo "安装完成。接下来按顺序测试:"
echo "  1) python3 sbus_test.py        # 验证SBUS遥控链路"
echo "  2) python3 -m unitree_webrtc_connect --help  # 或先跑库内交互示例验证连狗"
echo "  3) python3 main.py             # 联调"
