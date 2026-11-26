# -*- coding: utf-8 -*-
"""喊话音量: ALSA line-out 软件音量(遥控器App滑条 / 板上右拨杆都能调)
音量 0~100, 默认 30(保护 15W 喇叭; 功放旋钮请另固定在 50% 以下)
板子没有可用混音控件时降级: 状态照记, 写硬件失败只打日志"""
import subprocess


class Pa:
    def __init__(self, default_volume=30, log=print):
        self.log = log
        self.volume = default_volume
        self._bucket = default_volume // 5
        self.ctl = self._find_ctl()
        self.apply()
        log("喊话音量 %d%%, 混音控件: %s"
            % (self.volume, self.ctl or "无(仅记状态)"))

    def _find_ctl(self):
        """挑真正的音量控件: 必须 sget 输出带 % (排除 LINEOUT 这种纯开关), 名字打分取最优
        实测板子: 'LINEOUT'=纯开关(pswitch) 会误导, 'LINEOUT volume'(0-31) 才是音量"""
        try:
            out = subprocess.run(["amixer", "scontrols"], capture_output=True,
                                 text=True, timeout=5).stdout
        except Exception as e:
            self.log("音频混音器探测失败:", repr(e))
            return None
        cands = []
        for line in out.splitlines():
            if "'" not in line:
                continue
            name = line.split("'")[1]
            try:
                info = subprocess.run(["amixer", "sget", name], capture_output=True,
                                      text=True, timeout=5).stdout
            except Exception:
                continue
            if "%" not in info:            # 无音量能力(纯开关/mux), 跳过
                continue
            low = name.lower()
            score = 0
            if "lineout" in low or "line out" in low:
                score += 2
            if "volume" in low:
                score += 2
            if "digital" in low:
                score += 1
            cands.append((score, name))
        cands.sort(reverse=True)
        return cands[0][1] if cands else None

    def apply(self):
        """硬件控件固定 100%(实测 'LINEOUT volume' 改值不影响实际输出),
        真实音量由 pa_recv 里的软件增益(audioop.mul)控制"""
        if not self.ctl:
            return
        try:
            subprocess.run(["amixer", "sset", self.ctl, "100%"],
                           capture_output=True, timeout=5)
        except Exception as e:
            self.log("音量写入失败:", repr(e))

    def set_volume(self, v):
        """0~100; 只记状态(软件增益实时读取), 5% 一档打日志防刷屏"""
        v = max(0, min(100, int(v)))
        if v == self.volume:
            return
        self.volume = v
        if v // 5 != self._bucket:
            self._bucket = v // 5
            self.log("音量 -> %d%%" % v)
