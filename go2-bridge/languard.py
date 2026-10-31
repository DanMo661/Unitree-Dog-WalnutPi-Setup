# -*- coding: utf-8 -*-
"""局域网控制面守卫: 源 IP 白名单 + 共享令牌(2026-10-29 审查加固)。

背景：网络遥控(UDP 15010)、网页遥控(/ws)、音频三端口原先全部监听 0.0.0.0 且无鉴权，
同一 Wi-Fi 内任何设备都能伪造杆量解锁并开走狗、或远程喊话。这里做成"可开可关"，
默认值保持向后兼容（旧版 G12 App 不带令牌也能用），配了才强制：

  - allow 空 = 不限来源；非空 = 只接受这些网段/IP（"192.168.8.0/24" 或 "192.168.8.8"）
  - token 空 = 不校验；非空 = 请求必须带同一串（UDP 用 "tok" 字段，网页用 ?tok=）

调用方应调用 assert_configured(log, name)：两项都没配时打一条告警，避免"以为关了
其实全开"。校验失败只计数不抛异常，控制循环照常跑。
"""
import hmac
import ipaddress


class NetGuard:
    def __init__(self, allow=None, token="", log=None, name="guard"):
        self.allow = []
        for r in allow or []:
            try:
                self.allow.append(ipaddress.ip_network(str(r), strict=False))
            except ValueError:
                if log:
                    msg = "!! %s 白名单项无效已忽略: %r" % (name, r)
                    try:
                        log(msg, lvl="warn")
                    except TypeError:   # 文档签名允许 log=print(无 lvl 形参), 同 _reject
                        log(msg)
        self.token = token or ""
        self.rej = 0
        self.log = log
        self.name = name

    @property
    def open(self):
        """两项都没配 = 完全开放"""
        return not self.allow and not self.token

    def ip_ok(self, ip):
        if not self.allow:
            return True
        try:
            a = ipaddress.ip_address(str(ip))
        except ValueError:
            return self._reject("bad-ip %r" % (ip,))
        return True if any(a in n for n in self.allow) else self._reject("ip %s" % ip)

    def token_ok(self, tok):
        if not self.token:
            return True
        return True if hmac.compare_digest(str(tok or ""), self.token) else self._reject("token")

    def check(self, ip, tok=None):
        return self.ip_ok(ip) and self.token_ok(tok)

    def _reject(self, why):
        self.rej += 1
        if self.log and (self.rej in (1, 10, 100) or self.rej % 500 == 0):
            msg = "!! %s 拒绝未授权请求(%s), 累计 %d 次" % (self.name, why, self.rej)
            try:
                self.log(msg, lvl="warn")
            except TypeError:
                # 文档签名允许 log=print(无 lvl 形参) —— 降级为裸文本,
                # 绝不能让告警日志把接收线程炸死(10-31 测试桩实抓)
                self.log(msg)
        return False

    def stats(self):
        return {"guard_rej": self.rej}

    def assert_configured(self, log, hint=""):
        # 10-31 用户定案不再打"未设鉴权"告警: 遥控器(SBUS)是最高优先级控制源,
        # 网页/App 任何指令都越不过它, 开放端口不构成实际风险; 门禁本体不变,
        # 配了 allow/token 照常生效(languard 拦截逻辑零改动)。
        return
