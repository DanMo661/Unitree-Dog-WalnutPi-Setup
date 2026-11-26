# -*- coding: utf-8 -*-
"""dog2 板级差异覆盖(deploy 上传到板上与本文件同名生效; 见 config.py 尾部白名单)。

dog2 右声道输出级损坏(10-10 判死): PA_STEREO 必须 False —— pa_recv 播放/喊话
双路径都置零右声道, 只出左声道。变体 config.py 保持通用默认 True, 由这里收敛,
全新/重灌的板子随本目录一起部署即可拿到正确声道策略, 不依赖板上残留文件。
"""
OVERRIDES = {
    "PA_STEREO": False,
}
