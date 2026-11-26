package com.wjl.pasend;

import android.app.Activity;
import android.content.Intent;
import android.content.res.ColorStateList;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import java.net.DatagramSocket;

/**
 * 哨兵S1 主界面(v38 浅色壳): 全屏 WebView 加载板端调试台, 无系统标题栏
 * 底部 FlClash 分段控件: 喊话台/气体检测/动作/网络控制, 选中段=蓝实心
 * 板子地址改由网页设置页下发(window.__appCmd → 600ms 轮询带回), 原生地址按钮已撤
 * 权限引导(麦克风/悬浮窗/存储)收进喊话台段点击流程; tts 隐藏入口保留
 */
public class PaSend extends Activity {

    // 板端 SBUS 调试台地址: 跟随 ⚙ 设置的板子地址(prefs 键 board_ip), 每次加载运行时读
    String consoleUrl() {
        return "http://" + PaService.boardIp(this) + ":8000/sbus";
    }

    LinearLayout root;
    FrameLayout holder;
    WebView web;
    View errPage;
    TextView errUrlText;               // 错误页里显示当前控制台地址, 改板子地址后刷新
    TextView goBtn;                    // = segBtns[0] 喊话台段(v38 起并入分段组)
    TextView[] segBtns;                // 底部分段组: 喊话台/气体检测/动作/网络控制(常灰底)
    android.os.Handler pollHandler;
    Runnable pollRun;
    android.app.Dialog curDialog;      // 动作面板引用
    android.app.Dialog confirmDialog;  // 二次确认弹窗引用(可与面板叠加, onDestroy 各自 dismiss)
    android.os.Handler retryHandler;   // 错误页后台自动重试
    Runnable retryRun;
    boolean mainFrameFailed = false;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        // 隐藏入口: adb am start --es tts "文字" 直接合成(调试/远程用)
        String ttsText = getIntent() == null ? null : getIntent().getStringExtra("tts");
        if (ttsText != null && ttsText.length() > 0) {
            Intent si = new Intent(this, PaService.class);
            si.putExtra("tts", ttsText);
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(si);
            else startService(si);
            finish();
            return;
        }
        // v38 浅色壳: 去系统标题栏, 状态栏/导航栏融入网页浅底(上下不再有深色条)
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        getWindow().setStatusBarColor(PaTheme.LBG);
        getWindow().setNavigationBarColor(PaTheme.LBG);
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        buildUi();
    }

    @Override
    protected void onResume() {
        super.onResume();
        refresh();
        startActionPoll();
        if (retryHandler != null && retryRun != null) {
            retryHandler.removeCallbacks(retryRun);
            retryHandler.postDelayed(retryRun, 5000);   // 回前台恢复自动重试
        }
    }

    @Override
    protected void onPause() {
        if (pollHandler != null && pollRun != null) pollHandler.removeCallbacks(pollRun);
        if (retryHandler != null && retryRun != null) retryHandler.removeCallbacks(retryRun);
        super.onPause();
    }

    /** 每 600ms 从调试台页面读 App 指令(网页设置页经 window.__appCmd 下发, 如改板子地址) */
    void startActionPoll() {
        if (pollHandler == null) pollHandler = new android.os.Handler();
        if (pollRun == null) {
            pollRun = new Runnable() {
                public void run() {
                    if (web != null) {
                        web.evaluateJavascript(
                            "(function(){var c=window.__appCmd;"
                            + "try{delete window.__appCmd}catch(e){window.__appCmd=undefined}"
                            + "return c?JSON.stringify(c):''})()",
                            new android.webkit.ValueCallback<String>() {
                                public void onReceiveValue(String v) {
                                    if (v == null || v.length() < 3) return;
                                    // 回调是 JSON 再编码的字符串, 剥引号反斜杠后形如 {setIp:192.168.8.53}
                                    String s = v.replace("\"", "").replace("\\", "");
                                    java.util.regex.Matcher m = java.util.regex.Pattern
                                            .compile("setIp[ :=]+([0-9]+\\.[0-9.]+)").matcher(s);
                                    if (m.find()) applyBoardIp(m.group(1));
                                }
                            });
                    }
                    if (pollHandler != null) pollHandler.postDelayed(this, 600);
                }
            };
        } else {
            pollHandler.removeCallbacks(pollRun);
        }
        pollHandler.post(pollRun);
    }

    @Override
    protected void onDestroy() {
        if (retryHandler != null && retryRun != null) retryHandler.removeCallbacks(retryRun);
        if (curDialog != null) { try { curDialog.dismiss(); } catch (Exception ig) {} }
        if (confirmDialog != null) { try { confirmDialog.dismiss(); } catch (Exception ig) {} }
        if (web != null) {
            web.loadUrl("about:blank");
            ((ViewGroup) web.getParent()).removeView(web);
            web.destroy();
        }
        super.onDestroy();
    }

    boolean hasMic() {
        return checkSelfPermission(android.Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED;
    }

    boolean hasOverlay() {
        return Settings.canDrawOverlays(this);
    }

    boolean hasStorage() {
        return checkSelfPermission(android.Manifest.permission.READ_EXTERNAL_STORAGE)
                == PackageManager.PERMISSION_GRANTED;
    }

    void buildUi() {
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(PaTheme.LBG);   // v38: 壳=网页浅底, 不再有深色上下边
        setContentView(root);
        // 控制台常亮: 握摇杆时没人碰屏幕, 熄屏会断 /ws 触发桥的自动趴下
        getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        // ===== WebView 区(错误页盖在上面) =====
        holder = new FrameLayout(this);
        root.addView(holder, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1));

        web = new WebView(this);
        web.setBackgroundColor(PaTheme.LCARD);
        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setSupportZoom(false);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String url) {
                return false;   // 站内跳转都在 WebView 里走
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                // 子资源失败不算(页面自己会处理), 只拦截主文档加载失败
                if (req.isForMainFrame()) {
                    mainFrameFailed = true;
                    errPage.setVisibility(View.VISIBLE);
                }
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                // 失败时保持错误页与 mainFrameFailed=true(交给 5s 自动重试), 只在成功时收错误页
                if (!mainFrameFailed) errPage.setVisibility(View.GONE);
            }
        });
        holder.addView(web, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        holder.addView(buildErrPage(), new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));

        // ===== 底部: FlClash 分段控件(浅色胶囊容器, 选中段=蓝实心白字白图标) =====
        LinearLayout bottom = new LinearLayout(this);
        bottom.setOrientation(LinearLayout.HORIZONTAL);
        bottom.setGravity(Gravity.CENTER_VERTICAL);
        GradientDrawable segBg = PaTheme.fill(PaTheme.LWELL, 999);
        segBg.setStroke(dp(1), PaTheme.LBORDER);
        bottom.setBackground(segBg);
        bottom.setPadding(dp(3), dp(3), dp(3), dp(3));
        LinearLayout.LayoutParams bottomLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        bottomLp.leftMargin = dp(8);
        bottomLp.rightMargin = dp(8);
        bottomLp.topMargin = dp(6);
        bottomLp.bottomMargin = dp(6);
        root.addView(bottom, bottomLp);

        String[] segNames = {"喊话台", "气体检测", "动作", "网络控制"};
        int[] segIcons = {R.drawable.ic_mic, R.drawable.ic_gas, R.drawable.ic_star,
                R.drawable.ic_pad};
        int[] segIconColors = {0xFF3B82F6, 0xFF10B981, 0xFFF59E0B, 0xFF6366F1};   // v40: 图标各染一色
        segBtns = new TextView[4];
        for (int i = 0; i < 4; i++) {
            final int idx = i;
            TextView b = mkBottomBtn(0x00000000, PaTheme.LTXT, segIcons[i], segNames[i]);
            b.setBackground(PaTheme.pill(PaTheme.LSEG));   // v39: 四段常灰底(蓝色选中态已按用户要求去掉)
            b.setPadding(dp(14), 0, dp(0), 0);             // 内容整体往右挪一点
            b.setCompoundDrawableTintList(ColorStateList.valueOf(segIconColors[i]));
            LinearLayout.LayoutParams sp = new LinearLayout.LayoutParams(0, dp(40), 1);
            if (i > 0) sp.leftMargin = dp(2);
            bottom.addView(b, sp);
            b.setOnClickListener(new View.OnClickListener() {
                public void onClick(View v) { segSelect(idx); }
            });
            segBtns[i] = b;
        }
        goBtn = segBtns[0];

        loadConsole();
    }

    /** 分段点击: 直接打开对应台面(v39 起无选中变色, 四段常灰) */
    void segSelect(int i) {
        if (i == 0) onGo();
        else if (i == 1) onGoGas();
        else if (i == 2) showTrickPanel();
        else startActivity(new Intent(this, ManualCtrlActivity.class));
    }

    /** 气体检测悬浮窗: 需悬浮窗权限, 拉起 PaService 的气体面板 */
    void onGoGas() {
        if (!hasOverlay()) {
            toast("气体悬浮窗需要悬浮窗权限");
            try {
                startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:" + getPackageName())));
            } catch (Exception e) {
                toast("请在系统设置里开启悬浮窗权限");
            }
            return;
        }
        Intent si = new Intent(this, PaService.class);
        si.putExtra("panel", "gas");
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(si);
        else startService(si);
        toast("气体检测悬浮窗已启动");
        moveTaskToBack(true);
    }

    /** 底部容器条按钮统一样式: 单色图标+文字, 单行截断(FLstyle: 字号不缩, 放不下就截) */
    TextView mkBottomBtn(int bg, int fg, int icon, String text) {
        TextView b = new TextView(this);
        b.setTextColor(fg);
        b.setTextSize(13);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setGravity(Gravity.CENTER);
        b.setBackground(bg == 0x00000000 ? null : PaTheme.pill(bg));
        b.setSingleLine(true);
        b.setEllipsize(android.text.TextUtils.TruncateAt.END);
        b.setCompoundDrawablePadding(dp(5));
        if (text != null) b.setText(text);
        if (icon != 0) b.setCompoundDrawablesWithIntrinsicBounds(icon, 0, 0, 0);
        return b;
    }

    // ===== 动作面板: 顺序/白名单与板端 config.TRICKS 一致; 音频=板上预设文件名(触发时先播) =====
    // 10-10 用户拍板: 只留两支舞+握手(Sit/Stretch/Content/Scrape/Heart 按钮删除, 防误触发)
    static final String[][] TRICKS = {
        {"摇摆舞",      "Dance1",      "24", "基米斗"},
        {"舞蹈",        "Dance2",      "39", "基米斗地主"},
        {"握手",        "Hello",       "4",  ""},
    };

    void showTrickPanel() {
        LinearLayout grid = new LinearLayout(this);
        grid.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(16);
        grid.setPadding(pad, pad, pad, dp(12));

        java.util.ArrayList<LinearLayout> rows = new java.util.ArrayList<LinearLayout>();
        for (int i = 0; i < TRICKS.length; i++) {
            if (i % 2 == 0) {
                LinearLayout row = new LinearLayout(this);
                row.setOrientation(LinearLayout.HORIZONTAL);
                rows.add(row);
                LinearLayout.LayoutParams rp = new LinearLayout.LayoutParams(
                        LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
                rp.bottomMargin = dp(10);
                grid.addView(row, rp);
            }
            final String label = TRICKS[i][0], name = TRICKS[i][1], audio = TRICKS[i][3];
            final int dur = Integer.parseInt(TRICKS[i][2]);

            TextView chip = new TextView(this);
            chip.setTextSize(14);
            chip.setGravity(Gravity.CENTER);
            // 两行两色: 名称行主文字, 时长行次文字 (FLstyle: 次要信息降档)
            // v42: 每支动作一个主题色圆点(与底栏图标同色系: 蓝/绿/橙)
            int[] TRICK_COLORS = {0xFF3B82F6, 0xFF10B981, 0xFFF59E0B};
            String l2 = label + (audio.length() > 0 ? " 🎵" : "");
            android.text.SpannableString ss = new android.text.SpannableString(
                    String.format("● %s\n%ds", l2, dur));
            ss.setSpan(new android.text.style.ForegroundColorSpan(TRICK_COLORS[i]),
                    0, 2, android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            ss.setSpan(new android.text.style.ForegroundColorSpan(PaTheme.LTXT),
                    2, 2 + l2.length(), android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            ss.setSpan(new android.text.style.ForegroundColorSpan(PaTheme.LTXT2),
                    2 + l2.length(), ss.length(), android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            chip.setText(ss);
            chip.setTypeface(Typeface.DEFAULT_BOLD);
            chip.setPadding(dp(6), dp(12), dp(6), dp(12));
            chip.setBackground(PaTheme.fill(PaTheme.LWELL, 14));
            LinearLayout.LayoutParams cp = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1);
            if (i % 2 == 1) cp.leftMargin = dp(10);
            rows.get(rows.size() - 1).addView(chip, cp);
            chip.setOnClickListener(new View.OnClickListener() {
                public void onClick(View v) { confirmTrick(label, name, dur, audio); }
            });
        }
        TextView tip = new TextView(this);
        tip.setText("全部动作需二次确认 · 触发条件: 已解锁+站立\n🎵 = 带配乐");
        tip.setTextColor(PaTheme.LTXT2);
        tip.setTextSize(12);
        tip.setGravity(Gravity.CENTER);
        tip.setPadding(0, dp(2), 0, 0);
        grid.addView(tip);

        // 自定义深色面板(替代系统 AlertDialog 的白色外框)
        final android.app.Dialog dlg = new android.app.Dialog(this);
        curDialog = dlg;
        dlg.requestWindowFeature(android.view.Window.FEATURE_NO_TITLE);
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setBackground(roundBg(PaTheme.LCARD, dp(18)));
        TextView title = new TextView(this);
        title.setText("狗的动作");
        title.setTextColor(PaTheme.LTXT);
        title.setTextSize(17);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setGravity(Gravity.CENTER);
        title.setPadding(0, dp(16), 0, dp(6));
        panel.addView(title);
        panel.addView(new android.widget.ScrollView(this) {{ addView(grid); }},
                new LinearLayout.LayoutParams(-1, -2));
        TextView close = new TextView(this);
        close.setText("关 闭");
        close.setTextColor(PaTheme.LTXT2);
        close.setTextSize(14);
        close.setGravity(Gravity.CENTER);
        close.setPadding(0, dp(10), 0, dp(14));
        close.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { dlg.dismiss(); }
        });
        panel.addView(close);
        dlg.setContentView(panel);
        android.view.Window win = dlg.getWindow();
        if (win != null) {
            win.setBackgroundDrawable(new android.graphics.drawable.ColorDrawable(0x00000000));
            win.setLayout(dp(340), -2);
        }
        dlg.show();
    }

    /** 深色圆角背景 */
    android.graphics.drawable.GradientDrawable roundBg(int color, int r) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(color);
        g.setCornerRadius(r);
        return g;
    }

    /** 所有动作统一二次确认 */
    void confirmTrick(final String label, final String name, final int dur, final String audio) {
        final android.app.Dialog dlg = new android.app.Dialog(this);
        confirmDialog = dlg;
        dlg.requestWindowFeature(android.view.Window.FEATURE_NO_TITLE);
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setBackground(roundBg(PaTheme.LCARD, dp(18)));
        panel.setPadding(dp(22), dp(20), dp(22), dp(16));

        TextView t = new TextView(this);
        t.setText("确认执行");
        t.setTextColor(PaTheme.LTXT);
        t.setTextSize(16);
        t.setTypeface(Typeface.DEFAULT_BOLD);
        t.setGravity(Gravity.CENTER);
        panel.addView(t);
        TextView m = new TextView(this);
        m.setText(String.format("狗将执行「%s」(%d 秒)%s", label, dur,
                audio.length() > 0 ? "\n同步播放配乐: " + audio : ""));
        m.setTextColor(PaTheme.LTXT2);
        m.setTextSize(14);
        m.setGravity(Gravity.CENTER);
        m.setPadding(0, dp(10), 0, dp(16));
        panel.addView(m);

        LinearLayout btns = new LinearLayout(this);
        btns.setOrientation(LinearLayout.HORIZONTAL);
        TextView cancel = mkDialogBtn("取消", PaTheme.LWELL, PaTheme.LTXT);
        TextView ok = mkDialogBtn("执 行", PaTheme.LBLUE, 0xFFFFFFFF);
        btns.addView(cancel, new LinearLayout.LayoutParams(0, dp(42), 1));
        LinearLayout.LayoutParams op = new LinearLayout.LayoutParams(0, dp(42), 1);
        op.leftMargin = dp(10);
        btns.addView(ok, op);
        panel.addView(btns);
        cancel.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { dlg.dismiss(); }
        });
        ok.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                dlg.dismiss();
                sendTrick(name);
            }
        });
        dlg.setContentView(panel);
        android.view.Window win = dlg.getWindow();
        if (win != null) {
            win.setBackgroundDrawable(new android.graphics.drawable.ColorDrawable(0x00000000));
            win.setLayout(dp(300), -2);
        }
        dlg.show();
    }

    TextView mkDialogBtn(String text, int bg, int fg) {
        TextView b = new TextView(this);
        b.setText(text);
        b.setTextColor(fg);
        b.setTextSize(15);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setGravity(Gravity.CENTER);
        b.setBackground(PaTheme.pill(bg));
        return b;
    }

    /** 配乐(可选) + TRICK <name> -> 板端 15001, 回包 toast
     *  配乐由板端联动(TRICK 成功才播), 这里只发 TRICK 一条, 免得被拒时音乐白放 */
    void sendTrick(final String name) {
        new Thread(new Runnable() {
            public void run() {
                String r;
                DatagramSocket s = null;
                try {
                    s = new DatagramSocket();
                    s.setSoTimeout(2000);
                    s.connect(java.net.InetAddress.getByName(PaService.boardIp(PaSend.this)), PaService.CTRL_PORT);
                    byte[] ob = ("TRICK " + name).getBytes();
                    s.send(new java.net.DatagramPacket(ob, ob.length));
                    // 按前缀匹配收包
                    r = "";
                    long t0 = System.currentTimeMillis();
                    while (System.currentTimeMillis() - t0 < 2500) {
                        byte[] rb = new byte[256];
                        java.net.DatagramPacket rp = new java.net.DatagramPacket(rb, rb.length);
                        try { s.receive(rp); } catch (Exception e) { break; }
                        String line = new String(rb, 0, rp.getLength(), "UTF-8");
                        if (line.startsWith("OK TRICK") || line.startsWith("ERR")) { r = line; break; }
                    }
                    if (r.length() == 0) r = "TIMEOUT";
                } catch (Exception e) {
                    r = "TIMEOUT";
                } finally {
                    try { if (s != null) s.close(); } catch (Exception ig) {}
                }
                final String resp = r;
                runOnUiThread(new Runnable() {
                    public void run() {
                        if (resp.startsWith("OK TRICK"))      toast("✅ " + name + " 已触发");
                        else if (resp.contains("LINKDOWN"))   toast("❌ 狗离线, 动作没发出去");
                        else if (resp.contains("NOTSTAND"))   toast("❌ 需先解锁并站立(或动作占用中)");
                        else if (resp.contains("BADTRICK"))   toast("❌ 不支持的动作");
                        else if (resp.contains("NOTREADY"))   toast("❌ 板端服务未就绪");
                        else if (resp.equals("TIMEOUT"))      toast("❌ 板子无响应, 检查 WiFi/板子");
                        else                                   toast("❌ 未知回包: " + resp);
                    }
                });
            }
        }).start();
    }

    /** 加载失败覆盖页: 原因提示 + 重试按钮 (FLstyle 空态范式: 图标井+标题+说明+胶囊钮) */
    View buildErrPage() {
        errPage = new FrameLayout(this);
        errPage.setBackgroundColor(PaTheme.LBG);
        errPage.setVisibility(View.GONE);

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER_HORIZONTAL);
        FrameLayout.LayoutParams bp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT);
        bp.gravity = Gravity.CENTER;
        ((FrameLayout) errPage).addView(box, bp);

        android.widget.ImageView et = new android.widget.ImageView(this);
        et.setImageResource(R.drawable.ic_sat);
        et.setImageTintList(android.content.res.ColorStateList.valueOf(PaTheme.LTXT2));
        et.setBackground(PaTheme.fill(PaTheme.LWELL, 999));
        int etPad = dp(26);
        et.setPadding(etPad, etPad, etPad, etPad);
        LinearLayout.LayoutParams etp = new LinearLayout.LayoutParams(dp(100), dp(100));
        box.addView(et, etp);

        TextView e2 = new TextView(this);
        e2.setText("连接不上调试台");
        e2.setTextColor(PaTheme.LTXT);
        e2.setTextSize(16);
        e2.setTypeface(Typeface.DEFAULT_BOLD);
        e2.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams e2p = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        e2p.topMargin = dp(16);
        box.addView(e2, e2p);

        TextView es = new TextView(this);
        errUrlText = es;
        es.setText(consoleUrl() + "\n\n检查板子是否开机、遥控器 WiFi 是否连上");
        es.setTextColor(PaTheme.LTXT2);
        es.setTextSize(13);
        es.setLineSpacing(dp(3), 1);
        es.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams esp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        esp.topMargin = dp(10);
        esp.bottomMargin = dp(22);
        box.addView(es, esp);

        TextView retry = new TextView(this);
        retry.setText("重  试");
        retry.setTextColor(0xFFFFFFFF);
        retry.setTextSize(15);
        retry.setTypeface(Typeface.DEFAULT_BOLD);
        retry.setGravity(Gravity.CENTER);
        retry.setPadding(dp(40), dp(12), dp(40), dp(12));
        retry.setBackground(PaTheme.pill(PaTheme.LBLUE));
        retry.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { loadConsole(); }
        });
        box.addView(retry);

        // 离线兜底: 网页设置页不可达时, 唯一能改板子地址的地方
        TextView ipBtn = new TextView(this);
        ipBtn.setText("改板子地址");
        ipBtn.setTextColor(PaTheme.LTXT);
        ipBtn.setTextSize(13);
        ipBtn.setGravity(Gravity.CENTER);
        ipBtn.setPadding(dp(28), dp(10), dp(28), dp(10));
        ipBtn.setBackground(PaTheme.fill(PaTheme.LWELL, 999));
        ipBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { showIpDialog(); }
        });
        LinearLayout.LayoutParams ipb = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        ipb.topMargin = dp(10);
        box.addView(ipBtn, ipb);
        return errPage;
    }

    void loadConsole() {
        mainFrameFailed = false;
        errPage.setVisibility(View.GONE);
        if (errUrlText != null)         // 板子地址可能刚改过, 错误页地址同步刷新
            errUrlText.setText(consoleUrl() + "\n\n检查板子是否开机、遥控器 WiFi 是否连上");
        web.loadUrl(consoleUrl());
        // 板子暂时不在线时每 5s 静默重试, 通了页面自动出现
        if (retryHandler == null) retryHandler = new android.os.Handler();
        if (retryRun == null) {
            retryRun = new Runnable() {
                public void run() {
                    if (mainFrameFailed) {
                        mainFrameFailed = false;
                        web.loadUrl(consoleUrl());
                    }
                    if (retryHandler != null) retryHandler.postDelayed(this, 5000);
                }
            };
        } else {
            retryHandler.removeCallbacks(retryRun);
        }
        retryHandler.postDelayed(retryRun, 5000);
    }

    /** ⚙ 手动改板子地址(错误页入口): 离线时网页设置页本身打不开, 这里兜底 */
    void showIpDialog() {
        final android.widget.EditText et = new android.widget.EditText(this);
        et.setText(PaService.boardIp(this));
        et.setSingleLine();
        android.app.AlertDialog d = new android.app.AlertDialog.Builder(this)
                .setTitle("板子地址 (IP)")
                .setView(et)
                .setPositiveButton("保存", new android.content.DialogInterface.OnClickListener() {
                    public void onClick(android.content.DialogInterface dg, int w) {
                        applyBoardIp(et.getText().toString().trim());
                    }
                })
                .setNegativeButton("取消", null)
                .create();
        d.show();
    }

    /** ⚙ 板子地址设置(网页设置页经 __appCmd 下发): 存 pa_prefs 键 board_ip, 全部目标运行时读它 */
    void applyBoardIp(String ip) {
        boolean ipOk = ip.matches("(\\d{1,3}\\.){3}\\d{1,3}");
        if (ipOk) {
            for (String seg : ip.split("\\.")) {
                if (Integer.parseInt(seg) > 255) { ipOk = false; break; }
            }
        }
        if (!ipOk) {
            toast("地址格式不对(每段 0~255), 例: " + PaService.DEFAULT_IP);
            return;
        }
        if (ip.equals(PaService.boardIp(this))) return;   // 没变不折腾
        getSharedPreferences("pa_prefs", MODE_PRIVATE)
                .edit().putString("board_ip", ip).apply();
        toast("板子地址已切换: " + ip);
        loadConsole();   // 立即按新地址重连控制台
    }

    int dp(float v) {
        return Math.round(getResources().getDisplayMetrics().density * v);
    }

    void refresh() {
        if (PaService.svcUp && hasMic() && hasOverlay()) {
            goBtn.setText("回悬浮窗");
        } else if (!hasMic()) {
            goBtn.setText("先授麦克风");
        } else if (!hasOverlay()) {
            goBtn.setText("先授悬浮窗");
        } else if (!hasStorage()) {
            goBtn.setText("先授存储");
        } else {
            goBtn.setText("喊话台");
        }
    }

    void onGo() {
        if (!hasMic()) {
            requestPermissions(new String[]{android.Manifest.permission.RECORD_AUDIO}, 1);
            return;
        }
        if (!hasOverlay()) {
            toast("在列表里找到「GO2控制台」并打开开关");
            try {
                startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:" + getPackageName())));
            } catch (Exception e) {
                try {
                    startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION));
                } catch (Exception e2) {
                    toast("请在系统设置里开启悬浮窗权限");
                }
            }
            return;
        }
        if (!hasStorage()) {
            requestPermissions(new String[]{
                    android.Manifest.permission.READ_EXTERNAL_STORAGE}, 2);
            return;
        }
        Intent si = new Intent(this, PaService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(si);
        else startService(si);
        toast("喊话悬浮窗已启动");
        moveTaskToBack(true);   // 退到后台但保持任务, 悬浮窗留在屏幕上
    }

    void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_SHORT).show();
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] p, int[] res) {
        if (res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED) {
            refresh();
        }
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }
}
