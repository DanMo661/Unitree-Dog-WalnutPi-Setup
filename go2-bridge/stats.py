# -*- coding: utf-8 -*-
"""SBUS 帧统计(SbusStats + 模块级单例 sbus_stats): rc_loop 喂帧, web_server 快照读, 零依赖只 stdlib"""
import collections
import threading
import time


class SbusStats:
    """SBUS 帧统计: rc_loop 经串口回调喂批次, web_server 快照读; 一把锁, 持锁极短。
    帧到达按串口 read 批次观测(同批多帧共享时间戳), 故帧间隔 = 批间隔/本批帧数
    (SBUS 固定周期, 均值即标称间隔; 断流时该值拉大, >25ms 记一次丢帧间隔)。
    接收机自身的 fs/lost 标志是丢帧的另一个独立真值来源。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.total = 0          # 总帧数
        self.fs_cnt = 0         # 失控标志帧数
        self.lost_cnt = 0       # 丢帧标志帧数
        self.gap_cnt = 0        # 帧间隔 >25ms 的次数
        self.last_iv = None     # 最近帧间隔 ms(无数据 None)
        self.fps = 0            # 1s 滑窗帧率
        self.hist = [0] * 60    # 最近 60s 每秒帧数(环形, 最新在 _hist_i)
        self.ch = None          # 最近一帧干净帧的 16 通道原始值
        self._ts = collections.deque()   # 1s 滑窗帧时间戳
        self._t_prev = None     # 上一批到达时刻
        self._sec0 = None       # 当前秒桶
        self._sec_n = 0         # 当前秒已收帧数
        self._hist_i = 59       # 环形最新槽位
        self._roll_n = 0        # 启动以来完成统计的秒数(趋势图启动零桶垫平用)

    def _roll(self, sec):
        """秒桶翻转: 结算旧秒并补零推进(链路停了也照推, 柱状图不说谎)。须持锁调用"""
        if self._sec0 is None:
            self._sec0 = sec
            return
        if sec == self._sec0:
            return
        self.hist[self._hist_i] = self._sec_n
        self._roll_n += min(sec - self._sec0, 60)   # 跳几秒算几秒: 少记会把真实零桶当未统计垫平(nitpick#2)
        for _ in range(min(sec - self._sec0, 60)):
            self._hist_i = (self._hist_i + 1) % 60
            self.hist[self._hist_i] = 0
        self._sec0 = sec
        self._sec_n = 0

    def feed(self, frames, now=None):
        """一批(同一次串口 read 解出的)帧喂入, frames: [(ch, fs, lost), ...]"""
        now = time.monotonic() if now is None else now
        with self.lock:
            for ch, fs, lost in frames:
                self.total += 1
                if fs:
                    self.fs_cnt += 1
                if lost:
                    self.lost_cnt += 1
                if not fs and not lost:
                    self.ch = list(ch)
                self._ts.append(now)
            while self._ts and self._ts[0] <= now - 1.0:
                self._ts.popleft()
            self.fps = len(self._ts)
            self._roll(int(now))
            self._sec_n += len(frames)
            if self._t_prev is not None:
                iv = (now - self._t_prev) * 1000.0 / len(frames)
                self.last_iv = round(iv, 1)
                if iv > 25.0:
                    self.gap_cnt += 1
            self._t_prev = now

    def snapshot(self, now=None):
        """供 /ws 推送: 顺带裁剪滑窗/推进秒桶, 返回可直接 json 化的字段"""
        now = time.monotonic() if now is None else now
        with self.lock:
            while self._ts and self._ts[0] <= now - 1.0:
                self._ts.popleft()
            self.fps = len(self._ts)
            self._roll(int(now))
            hist = self.hist[self._hist_i + 1:] + self.hist[:self._hist_i + 1]
            hist = hist[:-1]   # 丢"进行中的当前秒": 只计了部分帧, 画进趋势会形成右端规律性低点
            done = min(self._roll_n, len(hist))
            if done < len(hist):   # 启动未满一窗: 从未统计过的零桶按当前水平延展
                # 首个完成秒是串口爬坡值, done<=1 时拿它垫仍会整幅贴地(nitpick#3)
                pad = hist[-1] if done > 1 else int(self.fps)
                for i in range(len(hist) - done):
                    hist[i] = pad
            return {"total": self.total, "fs_cnt": self.fs_cnt,
                    "lost_cnt": self.lost_cnt, "gap_cnt": self.gap_cnt,
                    "last_iv": self.last_iv, "fps": self.fps,
                    "hist": hist, "ch": self.ch}


sbus_stats = SbusStats()
