# -*- coding: utf-8 -*-
"""喊话音频接收 v3: 预设文件板端播放 + 实时对讲 + 丢包统计
UDP:15000 对讲音频流(PCM S16LE 单声道, 采样率由 RATE 命令切换, 默认16k)
UDP:15001 控制(行协议): HELLO / VOL? / VOL n / LIST / PLAY 名 / STOP / DEL 名
                      / REN 旧 新 / TALK 1 / TALK 0 <发包数> / RATE n
UDP:15003 预设上传(二进制分块): UPB sid 名 大小 crc32hex / UPC sid 偏移 数据
                      / UPE sid (板端整文件校验后落盘)
预设文件存 ~/pa-files/<名>.pcm(16k 单声道 raw), 播放走软件增益(音量随 App 实时变)
对讲与预设播放互斥: TALK 1 自动停播放; 对讲中拒绝 PLAY(对讲优先)
"""
import audioop
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import zlib

FILES_DIR = os.path.join(os.path.expanduser("~"), "pa-files")
NAME_OK = re.compile(u"^[0-9A-Za-z_\\-\u4e00-\u9fff]{1,40}$")
CHUNK = 1024
MAX_SIZE = 8 * 1024 * 1024

# ---- 跨线程状态 ----
_rate = [16000]                       # 对讲采样率(RATE 命令改)
_respawn = [False]                    # 置位后 _loop 重建对讲 aplay
_talk = {"on": False, "last": 0.0, "pkt": 0}
_player = {"t": None, "stop": False, "name": None, "proc": None}
_close_now = [False]                  # TALK 0 置位: _loop 下个 tick 立即收对讲 aplay(不等2s静默)
_prestart = [False]                   # TALK 1 置位: 趁命令先于音频流的空档预启 aplay
_aplay = [None]                       # None=未探测 / False=缺失 / str=路径(缓存 which 结果)
_aplay_warn_t = [0.0]
trick_cb = None                       # bridge.main 注入 Dog.trick; 15001 TRICK 指令用
state_cb = None                       # bridge.main 注入: 返回 (rc_armed, mode, gear); 15001 "RC" 查询用


def start(pa, config, log=print):
    try:
        os.makedirs(FILES_DIR, exist_ok=True)
        for f in os.listdir(FILES_DIR):        # 清掉断电残留的半截上传
            if f.endswith(".part"):
                os.unlink(os.path.join(FILES_DIR, f))
    except Exception as e:
        log("音频库目录初始化失败:", repr(e))
    threading.Thread(target=_loop, args=(pa, config, log), daemon=True).start()
    threading.Thread(target=_ctrl_loop, args=(pa, config, log), daemon=True).start()
    threading.Thread(target=_up_loop, args=(pa, config, log), daemon=True).start()


# ============ 预设文件播放(板端本地, 走软件增益) ============

def _stop_player():
    _player["stop"] = True
    p = _player.get("proc")
    if p is not None:                          # kill 让阻塞中的 stdin.write 立刻出错退出
        try:
            p.kill()
        except Exception:
            pass
    t = _player["t"]
    if t is not None and t is not threading.current_thread():
        t.join(timeout=2)
    _player["t"] = None
    _player["name"] = None


def _player_thread(name, pa, log):
    path = os.path.join(FILES_DIR, name + ".pcm")
    proc = None
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            ["aplay", "-q", "-f", "S16_LE", "-c", "2", "-r", "16000", "-t", "raw"],
            stdin=subprocess.PIPE)
        _player["proc"] = proc
        with open(path, "rb") as f:
            while not _player["stop"]:
                b = f.read(4096)
                if not b:
                    break
                g = pa.volume / 100.0
                if g < 0.999:
                    try:
                        b = audioop.mul(b, 2, g)
                    except Exception:
                        pass
                # 信号只放 L, R 填零: 板子 R 模拟输出级硬件损坏(2026-10-10 判死)
                try:
                    b = audioop.tostereo(b, 2, 1, 0)
                except Exception:
                    pass
                proc.stdin.write(b)
        try:
            proc.stdin.close()
            proc.wait(timeout=3)
        except Exception:
            pass
    except Exception as e:
        if not _player["stop"]:       # 被 _stop_player kill 掉的属正常停播, 不算异常
            log("播放 %s 异常: %r" % (name, e))
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
    finally:
        if _player.get("t") is threading.current_thread():
            # 只清自己的引用: 被 _stop_player join 超时仍存活时, 别误清掉新起的播放器
            _player["proc"] = None
            _player["t"] = None
        if not _player["stop"]:
            log("播放完成: %s (%.1fs)" % (name, time.monotonic() - t0))


def _start_player(name, pa, log):
    if not NAME_OK.match(name):
        return "ERR BADNAME"
    if not os.path.isfile(os.path.join(FILES_DIR, name + ".pcm")):
        return "ERR NOFILE"
    _stop_player()
    _player["stop"] = False
    _player["name"] = name
    t = threading.Thread(target=_player_thread, args=(name, pa, log), daemon=True)
    _player["t"] = t
    t.start()
    return "OK PLAY " + name


def _crc_file(path):
    hv = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(65536)
            if not b:
                break
            hv = zlib.crc32(b, hv)
    return hv & 0xFFFFFFFF


def _list_files():
    """LIST 只 stat 不读盘: crc 用上传时算好落盘的 .crc 侧车(mtime 须新于音频本体),
    旧文件首次现算一次并补侧车——否则满库一次 LIST 全量读盘能把控制口卡死数秒"""
    out = []
    try:
        for f in sorted(os.listdir(FILES_DIR)):
            if not f.endswith(".pcm"):
                continue
            p = os.path.join(FILES_DIR, f)
            sp = p + ".crc"
            try:
                if os.path.isfile(sp) and os.path.getmtime(sp) >= os.path.getmtime(p):
                    with open(sp) as fh:
                        hv = int(fh.read().strip(), 16)
                else:
                    hv = _crc_file(p)
                    try:
                        with open(sp, "w") as fh:
                            fh.write("%08x" % hv)
                    except Exception:
                        pass
                out.append("%s|%d|%08x" % (f[:-4], os.path.getsize(p), hv))
            except Exception:
                pass
    except Exception:
        pass
    return out


# ============ 控制通道 ============

def _ctrl_loop(pa, config, log):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", config.PA_CTRL_PORT))
    except Exception as e:
        log("喊话控制端口 %d 被占, 停用: %r" % (config.PA_CTRL_PORT, e))
        return
    log("喊话控制就绪: UDP :%d (HELLO/VOL/LIST/PLAY/TALK/RATE)" % config.PA_CTRL_PORT)
    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except Exception:
            continue
        line = data.decode("utf-8", "replace").strip()
        parts = line.split(" ")
        reply = None
        if line == "HELLO":
            reply = "OK PA %d" % _rate[0]
        elif line == "RC":
            # App 手动控制面板轮询: 桥的真实解锁/模式/挡位(防 UI 与桥状态脱钩的盲区)
            if state_cb is None:
                reply = "ERR NOTREADY"
            else:
                try:
                    rc, mode, gear = state_cb()
                    reply = "RC %d %d %s" % (1 if rc else 0, mode, gear)
                except Exception:
                    reply = "ERR NOTREADY"
        elif line == "VOL?":
            reply = "VOL %d" % pa.volume
        elif parts[0] == "VOL":
            try:
                v = min(int(parts[1]), getattr(config, "PA_VOL_MAX", 100))
            except Exception:
                reply = "ERR BADVOL"
            else:
                pa.set_volume(v)
                reply = "OK VOL %d" % pa.volume
        elif line == "LIST":
            lines = _list_files()
            pkts, cur = [], ""
            for ln in lines:
                if len(cur) + len(ln) + 1 > 900:
                    pkts.append(cur)
                    cur = ln
                else:
                    cur = (cur + "\n" + ln) if cur else ln
            pkts.append(cur)
            for i, p in enumerate(pkts):
                try:
                    sock.sendto(("LST %d/%d\n%s" % (i + 1, len(pkts), p))
                                .encode("utf-8"), addr)
                except Exception:
                    pass
            continue
        elif parts[0] == "PLAY" and len(parts) >= 2:
            name = line[5:].strip()
            if _talk["on"]:
                reply = "BUSY TALK"
            elif not NAME_OK.match(name):
                reply = "ERR BADNAME"
            elif not os.path.isfile(os.path.join(FILES_DIR, name + ".pcm")):
                reply = "ERR NOFILE"
            else:
                # 回包先走: _start_player 内含旧播强停(join 最长2s), 别让它吃掉回包
                try:
                    sock.sendto(("OK PLAY " + name).encode("utf-8"), addr)
                except Exception:
                    pass
                _start_player(name, pa, log)
                log("播放请求: %s" % name)
                continue
        elif line == "STOP":
            try:
                sock.sendto(b"OK STOP", addr)
            except Exception:
                pass
            _stop_player()
            continue
        elif parts[0] == "TRICK" and len(parts) == 2:
            # App 动作面板 -> Dog.trick; 白名单 config.TRICKS, 配乐 config.TRICK_AUDIO
            # 配乐在动作触发成功后才播(被拒不放, 免得白放半分钟), 换动作先停旧配乐
            name = parts[1]
            dur = getattr(config, "TRICKS", {}).get(name)
            if dur is None:
                reply = "ERR BADTRICK"
            elif trick_cb is None:
                reply = "ERR NOTREADY"
            else:
                try:
                    r = trick_cb(name, dur)      # "OK" / "LINKDOWN" / "NOTSTAND"
                except Exception as e:
                    r = "NOTSTAND"
                    log("TRICK 异常:", name, repr(e))
                if r == "OK":
                    reply = "OK TRICK %s %d" % (name, dur)
                    log("App 动作: %s (%ds)" % (name, dur))
                    # 回包先走: 配乐启动含旧播强停(join 最长2s), 放后面会吃掉 App 的 2.5s 收包窗造成假"无响应"
                    try:
                        sock.sendto(reply.encode("utf-8"), addr)
                    except Exception:
                        pass
                    audio = getattr(config, "TRICK_AUDIO", {}).get(name, "")
                    if not audio:
                        pass                        # 该动作无配乐
                    elif _talk["on"]:
                        log("对讲中, 配乐跳过: %s" % audio)
                    elif not os.path.isfile(os.path.join(FILES_DIR, audio + ".pcm")):
                        log("配乐文件缺失, 无声起舞: %s" % audio)
                    else:
                        pr = _start_player(audio, pa, log)   # 内部先停旧播再起新播
                        if pr.startswith("OK"):
                            log("动作配乐: %s" % audio)
                        else:
                            log("动作配乐启动失败: %s (%s)" % (audio, pr))
                    continue
                elif r == "LINKDOWN":
                    reply = "ERR LINKDOWN"
                else:
                    reply = "ERR NOTSTAND"
        elif parts[0] == "DEL" and len(parts) >= 2:
            name = line[4:].strip()
            if not NAME_OK.match(name):
                reply = "ERR BADNAME"
            else:
                p = os.path.join(FILES_DIR, name + ".pcm")
                if os.path.isfile(p):
                    if name == _player["name"]:
                        _stop_player()
                    try:
                        os.unlink(p)
                        try:
                            os.unlink(p + ".crc")   # crc 侧车一并删
                        except OSError:
                            pass
                        reply = "OK DEL"
                        log("删除预设: %s" % name)
                    except Exception as e:
                        reply = "ERR " + repr(e)
                else:
                    reply = "ERR NOFILE"
        elif parts[0] == "BIRD" and len(parts) == 3:
            # 驱鸟硬件占位协议: BIRD ULTRA 1/0 超声波, BIRD STROBE 1/0 爆闪灯
            sub, on = parts[1], parts[2]
            if sub in ("ULTRA", "STROBE") and on in ("0", "1"):
                log("驱鸟 %s -> %s (硬件待接)" % (sub, "开" if on == "1" else "关"))
                reply = "OK BIRD %s %s" % (sub, on)
            else:
                reply = "ERR BADBIRD"
        elif parts[0] == "REN" and len(parts) == 3:
            a, b = parts[1], parts[2]
            if not (NAME_OK.match(a) and NAME_OK.match(b)):
                reply = "ERR BADNAME"
            else:
                pa_, pb = os.path.join(FILES_DIR, a + ".pcm"), os.path.join(FILES_DIR, b + ".pcm")
                if not os.path.isfile(pa_):
                    reply = "ERR NOFILE"
                else:
                    try:
                        os.rename(pa_, pb)
                        try:
                            os.rename(pa_ + ".crc", pb + ".crc")   # 侧车跟着改名
                        except OSError:
                            pass
                        reply = "OK REN"
                        log("重命名预设: %s -> %s" % (a, b))
                    except Exception as e:
                        reply = "ERR " + repr(e)
        elif line == "TALK 1" or line.startswith("TALK 1 "):
            _talk["on"] = True
            _talk["pkt"] = 0
            _talk["last"] = time.monotonic()   # 重置静默看门狗, 否则开播即误报"静默结束"
            _talk["red"] = "RED" in line       # App 冗余双发模式: 每包前置2字节seq, 发两遍
            _talk["lastseq"] = -1
            _talk["bytes"] = 0
            _prestart[0] = True                # 趁命令先于音频流的空档预启 aplay
            log("对讲开始 (rate=%d%s)" % (_rate[0], ", 冗余双发" if _talk["red"] else ""))
            try:
                sock.sendto(b"OK TALK", addr)
            except Exception:
                pass
            _stop_player()                     # 停预设播放(含 join), 回包已在前面走
            continue
        elif parts[0] == "TALK" and len(parts) >= 3 and parts[1] == "0":
            n = _talk["pkt"]
            _talk["on"] = False
            _close_now[0] = True               # _loop 下个 tick 立即收 aplay, 不占声卡 2.5s
            try:
                sent = int(parts[2])
            except Exception:
                sent = -1
            if sent > 0:
                loss = max(0, (sent - n) * 100 // sent)
                reply = "STAT recv=%d sent=%d loss=%d" % (n, sent, loss)
                log("对讲结束: 收 %d / 发 %d, 丢包 %d%%" % (n, sent, loss))
            else:
                reply = "STAT recv=%d" % n
                log("对讲结束: 收 %d" % n)
        elif parts[0] == "RATE" and len(parts) >= 2:
            try:
                r = int(parts[1])
            except Exception:
                r = 0
            if 4000 <= r <= 48000 and r % 1000 == 0:
                _rate[0] = r
                _respawn[0] = True             # 在播则重建 aplay, 空闲则下次起播生效
                reply = "OK RATE %d" % r
                log("对讲采样率 -> %d" % r)
            else:
                reply = "ERR BADRATE"
        else:
            reply = "ERR UNKNOWN"
        try:
            sock.sendto(reply.encode("utf-8"), addr)
        except Exception:
            pass


# ============ 对讲音频流 ============

_diag = [0]


def _close(proc):
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _spawn_aplay(rate, log):
    """起对讲 aplay; which 结果缓存(降级模式下别每包扫一遍 PATH)"""
    if _aplay[0] is None:
        _aplay[0] = shutil.which("aplay") or False
    if not _aplay[0]:
        if time.monotonic() - _aplay_warn_t[0] > 10:
            _aplay_warn_t[0] = time.monotonic()
            log("aplay 不存在, 音频仅接收不播放")
        return None
    try:
        return subprocess.Popen(
            ["aplay", "-q", "-f", "S16_LE", "-c", "2",
             "-r", str(rate), "-t", "raw"],
            stdin=subprocess.PIPE)
    except Exception as e:
        log("aplay 启动失败:", repr(e))
        return None


def _loop(pa, config, log):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", config.PA_UDP_PORT))
    except Exception as e:
        log("喊话接收端口 %d 被占, 停用: %r" % (config.PA_UDP_PORT, e))
        return
    sock.settimeout(0.5)
    log("喊话接收就绪: UDP :%d (PCM S16LE, 采样率可变)" % config.PA_UDP_PORT)
    proc = None
    while True:
        try:
            data, _ = sock.recvfrom(8192)
        except socket.timeout:
            # 链路静默 2s 视为对讲结束(App 崩溃/断链时不等 TALK 0)
            if _talk["on"] and time.monotonic() - _talk["last"] > 2.0:
                _talk["on"] = False
                log("对讲静默2s自动结束 (本段收包 %d)" % _talk["pkt"])
            idle = time.monotonic() - _talk["last"] > 2.0
            if _close_now[0]:            # TALK 0 要求立即收管(排干缓冲后自然退出)
                _close_now[0] = False
                idle = True
            # 对讲的 aplay 也要收掉, 否则一直占着声卡, 预设播放会 device busy
            if proc is not None and idle:
                _close(proc)
                proc = None
            if _prestart[0] and proc is None:   # TALK 1 预启: 趁空档先把 aplay fork 好
                _prestart[0] = False
                proc = _spawn_aplay(_rate[0], log)
            continue
        except Exception:
            continue
        if not data:
            continue
        if _talk["on"] and _talk.get("red") and len(data) > 2:
            seq = data[0] | (data[1] << 8)
            if seq == _talk.get("lastseq"):
                continue                       # 冗余双发的重复包, 丢一份不写 aplay
            _talk["lastseq"] = seq
            data = data[2:]
        _talk["pkt"] += 1
        _talk["last"] = time.monotonic()
        _talk["bytes"] = _talk.get("bytes", 0) + len(data)
        if _talk["on"] and _talk["pkt"] % 500 == 0:      # 16k≈10s 一条, 别刷TF卡
            log("对讲进行中: 已收包 %d, 平均 %dB/包 (16k=640 / 8k=320)"
                % (_talk["pkt"], _talk["bytes"] // 125))
            _talk["bytes"] = 0
        if _respawn[0]:
            _respawn[0] = False
            if proc is not None:
                _close(proc)
                proc = None
        if proc is None:
            _prestart[0] = False          # 包已到, 正常路径起进程, 预启标志作废
            proc = _spawn_aplay(_rate[0], log)
            if proc is None:
                continue
        # 软件增益: 每包实时读当前音量(aplay 的 ALSA 控件实测对输出无效)
        g = pa.volume / 100.0
        if g < 0.999:
            try:
                data = audioop.mul(data, 2, g)
            except Exception as e:
                if _diag[0] % 50 == 1:
                    log("增益失败: %r" % e)
        _diag[0] += 1
        if _diag[0] % 2000 == 1:                        # 增益诊断降频(原500≈每10s一条)
            try:
                log("增益 %.2f -> 幅度 %d (包%d)" % (g, audioop.max(data, 2), _diag[0]))
            except Exception:
                pass
        # 信号只放 L, R 填零: 板子 R 模拟输出级硬件损坏(2026-10-10 判死)
        try:
            data = audioop.tostereo(data, 2, 1, 0)
        except Exception:
            pass
        try:
            proc.stdin.write(data)
        except Exception:
            _close(proc)
            proc = None


# ============ 预设上传(UDP 分块 + 累计确认) ============

def _drop_session(sess, sid, why, log):
    s = sess.pop(sid, None)
    if s is None:
        return
    try:
        s["fh"].close()
    except Exception:
        pass
    try:
        os.unlink(s["part"])
    except Exception:
        pass
    log("上传会话放弃(%s): %s" % (why, s["name"]))


def _up_loop(pa, config, log):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", config.PA_UP_PORT))
    except Exception as e:
        log("上传端口 %d 被占, 停用: %r" % (config.PA_UP_PORT, e))
        return
    log("上传就绪: UDP :%d (预设文件 -> %s)" % (config.PA_UP_PORT, FILES_DIR))
    sess = {}
    sock.settimeout(1.0)
    while True:
        # 会话超时回收
        now = time.monotonic()
        for sid in list(sess):
            if now - sess[sid]["last"] > 60:
                _drop_session(sess, sid, "超时60s", log)
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except Exception:
            continue
        if len(data) < 2:
            continue
        cmd = data.split(b" ", 1)[0]
        if cmd == b"UPB":
            try:
                _, sid, name_b, size_b, crc_b = data.split(b" ", 4)
                name = name_b.decode("utf-8", "replace")
                size = int(size_b)
                crc = int(crc_b, 16)
            except Exception:
                _send(sock, addr, "ERR BADUPB")
                continue
            if not NAME_OK.match(name):
                _send(sock, addr, "ERR BADNAME")
            elif size <= 0 or size > MAX_SIZE:
                _send(sock, addr, "ERR BADSIZE")
            elif sess:
                _send(sock, addr, "BUSY UP")
            else:
                try:
                    n_files = len([f for f in os.listdir(FILES_DIR)
                                   if f.endswith(".pcm")])
                except Exception:
                    n_files = 0
                free_mb = shutil.disk_usage(FILES_DIR).free // (1024 * 1024)
                max_f = getattr(config, "PA_MAX_FILES", 200)
                min_free = getattr(config, "PA_MIN_FREE_MB", 1000)
                if n_files >= max_f:
                    _send(sock, addr, "ERR MAXFILES")
                    log("上传拒绝: 已有 %d 条(上限 %d)" % (n_files, max_f))
                elif free_mb < min_free:
                    _send(sock, addr, "ERR NOSPACE")
                    log("上传拒绝: 磁盘仅剩 %dMB" % free_mb)
                else:
                    part = os.path.join(FILES_DIR, name + ".part")
                    try:
                        fh = open(part, "wb")
                        sess[sid.decode()] = {"name": name, "size": size, "crc": crc,
                                              "part": part, "fh": fh, "got": set(),
                                              "prefix": 0, "last": time.monotonic()}
                        _send(sock, addr, "OK UPB")
                        log("上传开始: %s (%d B)" % (name, size))
                    except Exception as e:
                        _send(sock, addr, "ERR " + repr(e))
        elif cmd == b"UPC" and b" " in data:
            # 头三段是 ASCII, 第四段起是二进制音频数据
            seg = data.split(b" ", 3)
            if len(seg) != 4:
                continue
            sid = seg[1].decode("ascii", "replace")
            s = sess.get(sid)
            if s is None:
                _send(sock, addr, "ERR NOSID")
                continue
            try:
                off = int(seg[2])
            except Exception:
                continue
            payload = seg[3]
            if off % CHUNK != 0 or off >= s["size"] or len(payload) > CHUNK:
                continue                          # 脏包直接丢弃, 靠重传恢复
            try:
                s["fh"].seek(off)
                s["fh"].write(payload)
            except Exception as e:
                _send(sock, addr, "ERR " + repr(e))
                continue
            s["got"].add(off)
            s["last"] = time.monotonic()
            while s["prefix"] < s["size"] and s["prefix"] in s["got"]:
                s["prefix"] += CHUNK
            _send(sock, addr, "ACK %s %d" % (sid, s["prefix"]))
        elif cmd == b"UPE":
            sid = data.split(b" ")[1].decode("ascii", "replace") if len(data.split(b" ")) > 1 else ""
            s = sess.get(sid)
            if s is None:
                _send(sock, addr, "ERR NOSID")
                continue
            if s["prefix"] < s["size"]:
                _send(sock, addr, "FAIL incomplete %d/%d" % (s["prefix"], s["size"]))
                _drop_session(sess, sid, "不完整", log)
                continue
            try:
                s["fh"].flush()
                os.fsync(s["fh"].fileno())        # TF 卡: 落盘后再改名, 断电不留半截
                s["fh"].close()
            except Exception as e:
                log("上传落盘失败:", repr(e))
            hv = _crc_file(s["part"])
            final = os.path.join(FILES_DIR, s["name"] + ".pcm")
            if hv == s["crc"]:
                os.rename(s["part"], final)
                sess.pop(sid, None)
                try:
                    with open(final + ".crc", "w") as fh:   # crc 侧车: LIST 免再读全文件
                        fh.write("%08x" % hv)
                except Exception:
                    pass
                _send(sock, addr, "DONE " + sid)
                log("上传完成: %s (%d B, crc 校验通过)" % (s["name"], s["size"]))
            else:
                _send(sock, addr, "FAIL crc")
                _drop_session(sess, sid, "crc不符", log)


def _send(sock, addr, s):
    try:
        sock.sendto(s.encode("utf-8"), addr)
    except Exception:
        pass
