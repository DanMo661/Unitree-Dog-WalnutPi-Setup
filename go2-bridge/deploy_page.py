# -*- coding: utf-8 -*-
"""部署喊话安装页到板子 /home/orangepi/pa-page 并做成 systemd 常驻(go2-pa-page.service)
上传: index.html / PaSend.apk / version.json / go2-pa-page.service
用法: BRIDGE_PW=<板子密码> [BRIDGE_HOST=<板子IP>] python deploy_page.py
"""
import os
import pathlib
import sys

import paramiko

HOST = os.environ.get("BRIDGE_HOST", "192.168.123.50")
USER = os.environ.get("BRIDGE_USER", "pi")
PW = os.environ.get("BRIDGE_PW", "")
LOCAL = pathlib.Path(__file__).parent
APP = pathlib.Path(__file__).parent / "PaSend-v37-网络遥控.apk"   # 本版本目录内成品, 防构建目录落后时静默上架旧版
PAGE_DIR = os.environ.get("BRIDGE_PAGE_DIR", "/home/%s/pa-page" % USER)

INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>喊话台安装</title></head>
<body style="font-family:sans-serif;background:#0b1220;color:#e2e8f0;max-width:480px;margin:0 auto;padding:32px 16px">
<h2 style="color:#60a5fa">🎮 GO2控制台 v37</h2>
<p><a href="PaSend.apk" download style="display:block;text-align:center;padding:16px;background:#2563eb;color:#fff;border-radius:10px;font-size:18px;text-decoration:none">下载 PaSend.apk</a></p>
<ol style="line-height:1.9;color:#94a3b8">
<li>点上面按钮下载 APK, 点开安装(允许未知来源)</li>
<li>打开「GO2控制台」, 主界面即 SBUS 调试台(需连板子 WiFi)</li>
<li>点底部「喊话台」按钮, 授权后软件变悬浮窗</li>
<li>悬浮窗里点「控制台」即可切回主界面</li>
</ol>
<p style="color:#475569;font-size:13px">装好之后, 以后新版本直接在 App 里点「检查更新」, 不用再来这个网页。</p>
</body></html>
"""

VERSION_JSON = '{"version": 37, "file": "PaSend.apk"}\n'

UNIT = """[Unit]
Description=PA app download page (port 15080)
After=network.target

[Service]
User=%s
ExecStart=/usr/bin/python3 -m http.server 15080 --directory %s
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
""" % (USER, PAGE_DIR)


def main():
    if not PW:
        sys.exit("BRIDGE_PW 未设置")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PW, timeout=8)

    def run(cmd, timeout=30):
        _, o, e = c.exec_command(cmd, timeout=timeout)
        out = o.read().decode("utf-8", "replace").strip()
        err = e.read().decode("utf-8", "replace").strip()
        return out, err

    run("mkdir -p %s" % PAGE_DIR)
    sftp = c.open_sftp()
    for name, content in (("index.html", INDEX_HTML),
                          ("version.json", VERSION_JSON),
                          ("go2-pa-page.service", UNIT)):
        with sftp.open(PAGE_DIR + "/" + name, "w") as f:
            f.write(content.encode("utf-8"))
    sftp.put(str(APP), PAGE_DIR + "/PaSend.apk")
    sftp.close()
    run("sync")   # 板上 ext4 commit=600, 断电回滚写入 —— 强制立即落盘

    out, _ = run("ls -l %s/" % PAGE_DIR)
    print("页面目录:\n" + out)
    out, _ = run("sha256sum %s/PaSend.apk" % PAGE_DIR)
    print("板上 APK:", out)

    _, err = run("echo %s | sudo -S cp %s/go2-pa-page.service /etc/systemd/system/ && "
                 "sudo systemctl daemon-reload && sudo systemctl enable --now go2-pa-page"
                 % (PW, PAGE_DIR))
    if err and "password" not in err.lower():
        print("服务部署警告:", err[:200])
    out, _ = run("systemctl is-active go2-pa-page")
    print("go2-pa-page:", out)
    out, _ = run("curl -s -o /dev/null -w '%{http_code} %{size_download}B' "
                 "http://127.0.0.1:15080/")
    print("页面自检:", out)
    out, _ = run("curl -s -o /dev/null -w '%{http_code} %{size_download}B' "
                 "http://127.0.0.1:15080/PaSend.apk")
    print("APK 自检:", out)
    out, _ = run("curl -s http://127.0.0.1:15080/version.json")
    print("version.json:", out)
    c.close()
    print("页面部署完成")


if __name__ == "__main__":
    main()
