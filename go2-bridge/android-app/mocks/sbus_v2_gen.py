# -*- coding: utf-8 -*-
"""从 static/sbus.html 生成带数据桩的截图用页面 (v2 方案验证桩, 产物 mocks/sbus_v2.html)"""
import io, os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "static", "sbus.html")
DST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sbus_v2.html")

with io.open(SRC, encoding="utf-8") as f:
    html = f.read()

# 1) 假 WebSocket: connect() 拿到假对象, 不覆盖桩渲染的横幅
fake_ws = ("window.WebSocket = function(u){ this.readyState = 1; this.send = function(){}; "
           "this.close = function(){}; };\nconst $ = id")
assert "const $ = id" in html
html = html.replace("const $ = id", fake_ws, 1)

# 2) 数据桩 + hash 导航, 注入在主 script 末尾
stub = """
/* ==== 截图桩 (sbus_v2_gen.py 注入) ==== */
const STUB_S = {
  link:"up", rc:true, sbus:true, sbus_clean:true, net_ok:false, web:false,
  mode:2, action:"", action_left:0, gear:true,
  fps:101, total:123456, fs_cnt:35, lost_cnt:12, last_iv:9,
  hist: Array.from({length:60}, (_,i)=> 95+18*Math.sin(i/5)+(i%9===0?40:0)),
  ch: [1500,1180,300,920,1600,400,1100,820,300,1750,300,300,300,300,300,300]
};
const STUB_SYS = {
  ver:"5ad531e", last_stop:"clean", uptime_s:8100,
  pids:{bridge:1234, audio:1240, audio_age:35},
  audio:{ready:true, volume:80}, link_gen:2, ch_source:"sbus",
  sys:{load1:0.45, mem_pct:42, cpu_temp:65.2},
  dog:{fresh:true, mode:2, height:0.30, gait:"trot", vel:[0.2,0,0], age:0.4},
  ctl:{}, last_cmd:{name:"stand", ts:Math.floor(Date.now()/1000)-40, result:"OK"},
  last_events:[
    {kind:"rc",  ev:"解锁", ts:Math.floor(Date.now()/1000)-90, detail:"A 键"},
    {kind:"act", ev:"站立", ts:Math.floor(Date.now()/1000)-60, detail:""},
    {kind:"warn",ev:"sbus 丢帧", ts:Math.floor(Date.now()/1000)-30, detail:"5 帧"},
    {kind:"act", ev:"行走", ts:Math.floor(Date.now()/1000)-10, detail:""},
    {kind:"info",ev:"狗连接成功", ts:Math.floor(Date.now()/1000)-120, detail:"gen 2"}
  ]
};
render(STUB_S);
renderSys(STUB_SYS);
addEventListener("load", () => {
  const h = location.hash.slice(1);
  if (h){ const it = document.querySelector('.nav-item[data-page="' + h + '"]');
    if (it) it.click(); }
});
</script>
</body>"""
assert "</script>\n</body>" in html
html = html.replace("</script>\n</body>", stub, 1)

with io.open(DST, "w", encoding="utf-8", newline="\n") as f:
    f.write(html)
print("OK ->", DST)
