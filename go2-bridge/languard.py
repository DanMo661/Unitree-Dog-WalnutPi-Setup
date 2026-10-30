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
                    log("!! %s 白名单项无效已忽略: %r" % (name, r))
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
        if self.log and self.rej in (1, 10, 100) or (self.log and self.rej % 500 == 0):
            self.log("!! %s 拒绝未授权请求(%s), 累计 %d 次" % (self.name, why, self.rej))
        return False

    def stats(self):
        return {"guard_rej": self.rej}

    def assert_configured(self, log, hint=""):
        if self.open:
            log("!! %s 未设鉴权(源 IP 白名单/令牌都为空): 同网段任何设备都可操作 —— %s"
                % (self.name, hint or "建议在 config.py 配 allow 或 token"))
