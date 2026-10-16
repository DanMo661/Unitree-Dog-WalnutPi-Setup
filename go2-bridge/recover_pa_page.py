# -*- coding: utf-8 -*-
"""板子掉电/重启后恢复喊话安装页: 稳定性检查 -> 状态核对 -> 重建 /tmp/pa_test -> http.server 15080
用法: BRIDGE_PW=<板子密码> python recover_pa_page.py
"""
import hashlib
import os
import pathlib
import sys
import time

import paramiko

HOST = os.environ.get("BRIDGE_HOST", "192.168.137.50")
USER = "orangepi"
LOCAL = pathlib.Path(__file__).parent
APK = pathlib.Path(r"D:\ZcodeRely\android-build\PaSend.apk")

INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>喊话 App 安装</title></head>
<body style="font-family:sans-serif;max-width:480px;margin:40px auto;padding:0 16px">
<h2>对讲喊话 App 安装</h2>
<p><a href="PaSend.apk" download style="display:block;text-align:center;padding:16px;background:#2563eb;color:#fff;border-radius:8px;font-size:18px;text-decoration:none">下载 PaSend.apk</a></p>
<ol style="line-height:1.8">
<li>点上面按钮下载 APK</li>
<li>下载完成点开安装，允许「未知来源」</li>
<li>打开 App，允许麦克风权限</li>
<li>点「开始」对着遥控器说话，板子喇叭出声</li>
</ol>
<p style="color:#666">停止就再点一下按钮。音量用遥控器右拨杆调，D 键是喊话总开关（要拨到开）。</p>
</body></html>
"""


def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def main():
    pw = os.environ.get("BRIDGE_PW")
    if not pw:
        sys.exit("BRIDGE_PW 未设置")

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    # 阶段1: 连上且命令真的能跑通才算稳
    for i in range(15):
        try:
            c.connect(HOST, username=USER, password=pw, timeout=6,
                      banner_timeout=6, auth_timeout=6)
            _, o, _ = c.exec_command("echo ok", timeout=6)
            if o.read().decode().strip() == "ok":
                print("SSH 稳定 (第%d次尝试)" % (i + 1))
                break
            c.close()
        except Exception as e:
            try:
                c.close()
            except Exception:
                pass
        print("第%d次不稳: %s" % (i + 1, "重试中..."))
        time.sleep(6)
    else:
        sys.exit("15 次都连不稳, 板子状态存疑")

    def run(cmd, timeout=30):
        _, o, e = c.exec_command(cmd, timeout=timeout)
        out = o.read().decode("utf-8", "replace")
        err = e.read().decode("utf-8", "replace")
        return out.strip(), err.strip()

    # 阶段2: 核对三 IP + 服务 + 喊话接收
    out, _ = run("ip -4 addr show eth0 | grep inet")
    print("eth0 IP:\n  " + out.replace("\n", "\n  "))
    out, _ = run("systemctl is-active go2-bridge")
    print("go2-bridge:", out)
    out, _ = run("journalctl -u go2-bridge -n 8 --no-pager -o cat")
    print("journal 尾部:\n  " + out.replace("\n", "\n  "))

    # 阶段3: 重建安装页目录并上传
    run("mkdir -p /tmp/pa_test")
    sftp = c.open_sftp()
    idx = pathlib.Path("/tmp") / "_pa_index.html"
    idx.write_text(INDEX_HTML, encoding="utf-8")
    sftp.put(str(idx), "/tmp/pa_test/index.html")
    sftp.put(str(APK), "/tmp/pa_test/PaSend.apk")
    idx.unlink()
    sftp.close()
    out, _ = run("sha256sum /tmp/pa_test/PaSend.apk; ls -l /tmp/pa_test/")
    print("板上文件:\n  " + out.replace("\n", "\n  "))
    print("本地 APK sha256:", sha(APK))

    # 阶段4: 起服务(先清旧进程, http[.]server 防自杀匹配)
    run("pkill -f 'http[.]server 15080' ; sleep 1 ; true")
    run("nohup python3 -m http.server 15080 --directory /tmp/pa_test "
        ">/tmp/pa_test/http.log 2>&1 & sleep 1 ; true")
    time.sleep(2)
    out, _ = run("curl -s -o /dev/null -w '%{http_code} %{size_download}B' http://127.0.0.1:15080/")
    print("安装页自检:", out)
    out, _ = run("curl -s -o /dev/null -w '%{http_code} %{size_download}B' http://127.0.0.1:15080/PaSend.apk")
    print("APK 自检:", out)
    c.close()
    print("恢复完成")


if __name__ == "__main__":
    main()
