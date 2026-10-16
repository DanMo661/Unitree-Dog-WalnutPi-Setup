import socket, base64, os, struct, json, time

key = base64.b64encode(os.urandom(16)).decode()
s = socket.create_connection(("192.168.123.50", 8000), 5)
req = ("GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
       "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
       "Sec-WebSocket-Version: 13\r\n\r\n" % key)
s.sendall(req.encode())
buf = b""
while b"\r\n\r\n" not in buf:
    buf += s.recv(1024)

def rf(sock):
    h = sock.recv(2)
    if len(h) < 2:
        return None
    ln = h[1] & 0x7f
    if ln == 126:
        ln = struct.unpack(">H", sock.recv(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", sock.recv(8))[0]
    m = sock.recv(4) if (h[1] & 0x80) else b""
    d = b""
    while len(d) < ln:
        ch = sock.recv(ln - len(d))
        if not ch:
            break
        d += ch
    if m:
        d = bytes(b ^ m[i % 4] for i, b in enumerate(d))
    return d

t0 = time.time()
n = 0
while time.time() - t0 < 12:
    d = rf(s)
    if not d:
        break
    try:
        j = json.loads(d.decode())
    except Exception:
        continue
    j.pop("hist", None)
    if n % 2 == 0:   # 约1秒一条
        line = {k: j.get(k) for k in ("rc", "mode", "gear", "fs_cnt", "lost_cnt")}
        chs = j.get("ch") or j.get("channels") or j.get("chs")
        line["ch"] = chs
        print(time.strftime("%H:%M:%S"), json.dumps(line, ensure_ascii=False))
    n += 1
