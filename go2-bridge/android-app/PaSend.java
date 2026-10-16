package com.wjl.pasend;

import android.app.Activity;
import android.content.Intent;
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
 * GO2控制台 v24 主界面: 全屏 WebView 加载板端 SBUS 调试台
 * 底部「喊话台」大按钮 → 拉起 PaService 悬浮窗并退后台(软件变悬浮窗, 可互相切换)
 * 权限引导(麦克风/悬浮窗/存储)收进按钮点击流程; tts 隐藏入口保留
 */
public class PaSend extends Activity {

    // 板端 SBUS 调试台地址(实测连通的板子 IP)
    static final String CONSOLE_URL = "http://192.168.144.10:8000/sbus";

    LinearLayout root;
    FrameLayout holder;
    WebView web;
    View errPage;
    TextView goBtn;
    TextView actionBar;
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

    /** 每 600ms 从调试台页面读当前动作(页面 window.lastStatus 由 /ws 更新) */
    void startActionPoll() {
        if (pollHandler == null) pollHandler = new android.os.Handler();
        if (pollRun == null) {
            pollRun = new Runnable() {
                public void run() {
                    if (web != null) {
                        web.evaluateJavascript(
                            "(window.lastStatus&&window.lastStatus.action)?window.lastStatus.action+'|'+(window.lastStatus.action_left||0)+'|'+(window.lastStatus.link||''):''",
                            new android.webkit.ValueCallback<String>() {
                                public void onReceiveValue(String v) {
                                    boolean show = false;
                                    if (v != null && v.length() > 3) {
                                        String[] sp = v.replace("\"", "").split("\\|");
                                        // 页面数据会因 WS 断连冻结, 链路非 up 时别挂假动作条
                                        if (sp.length > 2 && !"up".equals(sp[2])) {
                                            actionBar.setVisibility(View.GONE);
                                            return;
                                        }
                                        String label = sp[0];
                                        for (String[] t : TRICKS) {
                                            if (t[1].equals(label)) { label = t[0]; break; }
                                        }
                                        double left = 0;
                                        try { left = Double.parseDouble(sp.length > 1 ? sp[1] : "0"); } catch (Exception e) {}
                                        actionBar.setText("🤸 正在播放: " + label
                                                + String.format(" (剩 %.1fs)", Math.max(0, left)));
                                        show = true;
                                    }
                                    actionBar.setVisibility(show ? View.VISIBLE : View.GONE);
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
        GradientDrawable bg = new GradientDrawable(
                GradientDrawable.Orientation.TOP_BOTTOM,
                new int[]{0xFF101B33, 0xFF060B16});
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackground(bg);
        setContentView(root);
        // 控制台常亮: 握摇杆时没人碰屏幕, 熄屏会断 /ws 触发桥的自动趴下
        getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        // ===== 顶部: 动作播放提示条(从调试台页面状态轮询) =====
        actionBar = new TextView(this);
        actionBar.setTextColor(0xFFFDE68A);
        actionBar.setTextSize(13);
        actionBar.setTypeface(Typeface.DEFAULT_BOLD);
        actionBar.setGravity(Gravity.CENTER);
        actionBar.setPadding(dp(8), dp(6), dp(8), dp(6));
        actionBar.setBackground(roundBg(0xFF78350F, dp(8)));
        LinearLayout.LayoutParams ap = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        ap.leftMargin = dp(10); ap.rightMargin = dp(10); ap.topMargin = dp(6);
        actionBar.setLayoutParams(ap);
        actionBar.setVisibility(View.GONE);
        root.addView(actionBar);

        // ===== WebView 区(错误页盖在上面) =====
        holder = new FrameLayout(this);
        root.addView(holder, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1));

        web = new WebView(this);
        web.setBackgroundColor(0xFF060B16);
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

        // ===== 底部: 喊话悬浮窗 | 驱鸟悬浮窗 | 动作 | 手动控制 四个并排紧凑按钮 =====
        LinearLayout bottom = new LinearLayout(this);
        bottom.setOrientation(LinearLayout.HORIZONTAL);
        bottom.setPadding(dp(14), dp(8), dp(14), dp(14));
        root.addView(bottom, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        goBtn = mkBottomBtn(0xFF3B82F6, 0xFF1D4ED8);
        bottom.addView(goBtn, new LinearLayout.LayoutParams(0, dp(46), 1));

        TextView birdBtn = mkBottomBtn(0xFFF59E0B, 0xFFD97706);
        birdBtn.setText("🐦  驱鸟悬浮窗");
        LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(0, dp(46), 1);
        bp.leftMargin = dp(8);
        bottom.addView(birdBtn, bp);

        TextView actBtn = mkBottomBtn(0xFF7C3AED, 0xFF5B21B6);
        actBtn.setText("🤸  动作");
        LinearLayout.LayoutParams acp = new LinearLayout.LayoutParams(0, dp(46), 1);
        acp.leftMargin = dp(8);
        bottom.addView(actBtn, acp);

        TextView manBtn = mkBottomBtn(0xFF059669, 0xFF047857);
        manBtn.setText("🎮  手动控制");
        LinearLayout.LayoutParams mp = new LinearLayout.LayoutParams(0, dp(46), 1);
        mp.leftMargin = dp(8);
        bottom.addView(manBtn, mp);

        goBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { onGo(); }
        });
        birdBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { onGoBird(); }
        });
        actBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { showTrickPanel(); }
        });
        manBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { startActivity(new Intent(PaSend.this, ManualCtrlActivity.class)); }
        });

        loadConsole();
    }

    /** 驱鸟悬浮窗: 需悬浮窗权限, 拉起 PaService 的驱鸟面板 */
    void onGoBird() {
        if (!hasOverlay()) {
            toast("驱鸟悬浮窗需要悬浮窗权限");
            try {
                startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:" + getPackageName())));
            } catch (Exception e) {
                toast("请在系统设置里开启悬浮窗权限");
            }
            return;
        }
        Intent si = new Intent(this, PaService.class);
        si.putExtra("panel", "bird");
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(si);
        else startService(si);
        toast("驱鸟悬浮窗已启动");
        moveTaskToBack(true);
    }

    /** 底部并排按钮的统一样式 */
    TextView mkBottomBtn(int c0, int c1) {
        TextView b = new TextView(this);
        b.setTextColor(0xFFFFFFFF);
        b.setTextSize(15);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setGravity(Gravity.CENTER);
        GradientDrawable g = new GradientDrawable(
                GradientDrawable.Orientation.TL_BR, new int[]{c0, c1});
        g.setCornerRadius(dp(14));
        b.setBackground(g);
        return b;
    }

    // ===== 动作面板: 顺序/白名单与板端 config.TRICKS 一致; 音频=板上预设文件名(触发时先播) =====
    // 10-10 用户拍板: 只留两支舞+握手(Sit/Stretch/Content/Scrape/Heart 按钮删除, 防误触发)
    static final String[][] TRICKS = {
        {"💃 摇摆舞",     "Dance1",      "24", "基米斗"},
        {"🕺 舞蹈",       "Dance2",      "39", "基米斗地主"},
        {"👋 握手",       "Hello",       "4",  ""},
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
            chip.setTextColor(0xFFE2E8F0);
            chip.setTextSize(14);
            chip.setGravity(Gravity.CENTER);
            chip.setTypeface(Typeface.DEFAULT_BOLD);
            String l2 = label + (audio.length() > 0 ? " 🎵" : "");
            chip.setText(String.format("%s\n%ds", l2, dur));
            chip.setPadding(dp(6), dp(12), dp(6), dp(12));
            GradientDrawable g = new GradientDrawable();
            g.setColor(0xFF1E293B);
            g.setCornerRadius(dp(12));
            g.setStroke(dp(1), 0xFF334155);
            chip.setBackground(g);
            LinearLayout.LayoutParams cp = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1);
            if (i % 2 == 1) cp.leftMargin = dp(10);
            rows.get(rows.size() - 1).addView(chip, cp);
            chip.setOnClickListener(new View.OnClickListener() {
                public void onClick(View v) { confirmTrick(label, name, dur, audio); }
            });
        }
        TextView tip = new TextView(this);
        tip.setText("全部动作需二次确认 · 触发条件: 已解锁+站立\n🎵 = 带配乐");
        tip.setTextColor(0xFF64748B);
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
        panel.setBackground(roundBg(0xFF0F172A, dp(18)));
        TextView title = new TextView(this);
        title.setText("🤸 狗的动作");
        title.setTextColor(0xFF60A5FA);
        title.setTextSize(17);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setGravity(Gravity.CENTER);
        title.setPadding(0, dp(16), 0, dp(6));
        panel.addView(title);
        panel.addView(new android.widget.ScrollView(this) {{ addView(grid); }},
                new LinearLayout.LayoutParams(-1, -2));
        TextView close = new TextView(this);
        close.setText("关 闭");
        close.setTextColor(0xFF94A3B8);
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
        panel.setBackground(roundBg(0xFF0F172A, dp(18)));
        panel.setPadding(dp(22), dp(20), dp(22), dp(16));

        TextView t = new TextView(this);
        t.setText("确认执行");
        t.setTextColor(0xFFFBBF24);
        t.setTextSize(16);
        t.setTypeface(Typeface.DEFAULT_BOLD);
        t.setGravity(Gravity.CENTER);
        panel.addView(t);
        TextView m = new TextView(this);
        m.setText(String.format("狗将执行「%s」(%d 秒)%s", label, dur,
                audio.length() > 0 ? "\n同步播放配乐: " + audio : ""));
        m.setTextColor(0xFFCBD5E1);
        m.setTextSize(14);
        m.setGravity(Gravity.CENTER);
        m.setPadding(0, dp(10), 0, dp(16));
        panel.addView(m);

        LinearLayout btns = new LinearLayout(this);
        btns.setOrientation(LinearLayout.HORIZONTAL);
        TextView cancel = mkDialogBtn("取消", 0xFF334155, 0xFF94A3B8);
        TextView ok = mkDialogBtn("执 行", 0xFF1D4ED8, 0xFFFFFFFF);
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
        b.setBackground(roundBg(bg, dp(12)));
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
                    s.connect(java.net.InetAddress.getByName(PaService.DST_IP), PaService.CTRL_PORT);
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

    /** 加载失败覆盖页: 原因提示 + 重试按钮 */
    View buildErrPage() {
        errPage = new FrameLayout(this);
        errPage.setBackgroundColor(0xFF0B1220);
        errPage.setVisibility(View.GONE);

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER_HORIZONTAL);
        FrameLayout.LayoutParams bp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT);
        bp.gravity = Gravity.CENTER;
        ((FrameLayout) errPage).addView(box, bp);

        TextView et = new TextView(this);
        et.setText("⚠  连接不上调试台");
        et.setTextColor(0xFFF87171);
        et.setTextSize(18);
        et.setTypeface(Typeface.DEFAULT_BOLD);
        et.setGravity(Gravity.CENTER);
        box.addView(et);

        TextView es = new TextView(this);
        es.setText(CONSOLE_URL + "\n\n检查板子是否开机、遥控器 WiFi 是否连上");
        es.setTextColor(0xFF64748B);
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
        retry.setTextColor(0xFF60A5FA);
        retry.setTextSize(15);
        retry.setTypeface(Typeface.DEFAULT_BOLD);
        retry.setGravity(Gravity.CENTER);
        retry.setPadding(dp(40), dp(12), dp(40), dp(12));
        GradientDrawable rg = new GradientDrawable();
        rg.setColor(0x00000000);
        rg.setCornerRadius(dp(12));
        rg.setStroke(dp(1), 0xFF3B82F6);
        retry.setBackground(rg);
        retry.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { loadConsole(); }
        });
        box.addView(retry);
        return errPage;
    }

    void loadConsole() {
        mainFrameFailed = false;
        errPage.setVisibility(View.GONE);
        web.loadUrl(CONSOLE_URL);
        // 板子暂时不在线时每 5s 静默重试, 通了页面自动出现
        if (retryHandler == null) retryHandler = new android.os.Handler();
        if (retryRun == null) {
            retryRun = new Runnable() {
                public void run() {
                    if (mainFrameFailed) {
                        mainFrameFailed = false;
                        web.loadUrl(CONSOLE_URL);
                    }
                    if (retryHandler != null) retryHandler.postDelayed(this, 5000);
                }
            };
        } else {
            retryHandler.removeCallbacks(retryRun);
        }
        retryHandler.postDelayed(retryRun, 5000);
    }

    int dp(float v) {
        return Math.round(getResources().getDisplayMetrics().density * v);
    }

    void refresh() {
        if (PaService.svcUp && hasMic() && hasOverlay()) {
            goBtn.setText("🎙  回到喊话悬浮窗");
        } else if (!hasMic()) {
            goBtn.setText("🎙  喊话悬浮窗 · 先授麦克风");
        } else if (!hasOverlay()) {
            goBtn.setText("🎙  喊话悬浮窗 · 先授悬浮窗");
        } else if (!hasStorage()) {
            goBtn.setText("🎙  喊话悬浮窗 · 先授存储");
        } else {
            goBtn.setText("🎙  喊话悬浮窗");
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
