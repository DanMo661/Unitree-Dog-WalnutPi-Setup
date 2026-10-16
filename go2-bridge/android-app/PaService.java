package com.wjl.pasend;

import android.app.AlertDialog;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.graphics.PixelFormat;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaCodec;
import android.media.MediaExtractor;
import android.media.MediaFormat;
import android.media.MediaRecorder;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.OpenableColumns;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.URL;
import java.nio.ByteOrder;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * 喊话台 v6 悬浮窗服务: 挂在摄像头画面上, 边控狗边用
 * 音量±按钮 / 开始喊话 / 开始录音(60s上限) / 录音库(播放/试听/删除/加文件/检查更新)
 * 快捷芯片一键播放; 面板空白处可拖动+位置记忆; 自建下载器更新
 */
public class PaService extends Service {

    static final int APP_VERSION = 35;
    static final String DST_IP = "192.168.144.10";
    static final int AUDIO_PORT = 15000;
    static final int CTRL_PORT = 15001;
    static final int UP_PORT = 15003;            // 预设文件上传(分块+累计确认)
    static final int CHUNK = 1024;               // 上传分块字节(与板端一致)
    static final int VOL_MAX = 100;              // 09-30 用户解除 3W 喇叭 30% 限制, 可拉满
    static final int BOARD_MAX_FILES = 200;      // 与板端 PA_MAX_FILES 一致
    static final int LIVE_BOOST = 3;             // 对讲麦克风增益(整数倍, 带削顶; 喇叭换 3W 后喊话音巨低的补偿)
    static final int PAGE_PORT = 15080;
    static final int RATE = 16000;
    static final int FRAME = 640;                // 40ms @16k 单声道
    static final long REC_MAX_BYTES = 1920000L;  // 录音 60s 上限(32000B/s)

    static final int M_IDLE = 0, M_LIVE = 1, M_REC = 2, M_PLAY = 3, M_AUD = 4;

    static PaService inst;
    static boolean svcUp = false;

    volatile int mode = M_IDLE;
    String playingName = null;
    int curVol = 25;                      // 与板端默认一致; 上限 VOL_MAX
    volatile int pendingVol = -1;         // 待发送音量(-1=无); 连点只留最新值
    Thread workThread, probeThread;
    final ExecutorService ctrlPool = Executors.newSingleThreadExecutor();
    final ExecutorService upPool = Executors.newSingleThreadExecutor();   // 上传/同步串行
    final java.util.ArrayDeque<String> logs = new java.util.ArrayDeque<String>();
    final Object logLock = new Object();
    final java.util.HashSet<String> uploading = new java.util.HashSet<String>();
    final java.util.HashSet<String> autoPlay = new java.util.HashSet<String>();
    boolean firstSync = false;
    boolean volSynced = false;            // 板端音量只同步一次(持续回读会打回用户刚调的值)
    volatile String lastUpErr = "";       // 最近一次上传被拒的原因(ERR MAXFILES 等)
    int talkRate = 8000;                  // 对讲采样率: 默认"省"(链路实测16k/8k丢包率同, 省一半带宽)
    volatile int liveSent = 0;            // 本轮对讲已发包数(算丢包率用)
    android.net.wifi.WifiManager.WifiLock wifiLock;
    Runnable playFinish;
    final Handler ui = new Handler(Looper.getMainLooper());
    SharedPreferences prefs;
    JSONArray presets;

    // 悬浮窗
    WindowManager wm;
    LinearLayout panel;
    TextView bubble;
    WindowManager.LayoutParams panelLp, bubbleLp;
    boolean expanded = true;
    TextView statusText, volText, btnTalk, btnRec, hint, updBtn, rateBtn;
    FrameLayout levelTrack;
    View levelFill;
    LinearLayout chipsRow;

    static final Pattern VER_PAT = Pattern.compile("\"version\"\\s*:\\s*(\\d+)");
    volatile boolean dlBusy = false;
    int latestVer = 0;

    // ================= 生命周期 =================

    @Override
    public IBinder onBind(Intent i) { return null; }

    @Override
    public void onCreate() {
        super.onCreate();
        inst = this;
        svcUp = true;
        prefs = getSharedPreferences("pa_prefs", Context.MODE_PRIVATE);
        talkRate = prefs.getInt("talkRate", 8000);
        loadPresets();
        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        startFg();
        buildBubble();
        buildPanel();      // 只构建; 显示哪个面板由 onStartCommand 的 extra 决定
        startProbe();
    }

    void startFg() {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        String chId = "pa";
        Notification.Builder nb;
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(new NotificationChannel(chId, "喊话台",
                    NotificationManager.IMPORTANCE_LOW));
            nb = new Notification.Builder(this, chId);
        } else {
            nb = new Notification.Builder(this);
        }
        nb.setSmallIcon(android.R.drawable.ic_btn_speak_now)
          .setContentTitle("喊话台运行中")
          .setContentText("悬浮窗已就绪");
        startForeground(1, nb.build());
    }

    @Override
    public int onStartCommand(Intent i, int flags, int id) {
        startFg();   // API26+: 每次 startForegroundService 后必须配对 startForeground, 否则数秒后进程崩
        if (i != null && i.hasExtra("tts")) {
            final String t = i.getStringExtra("tts");
            ui.post(new Runnable() { public void run() { ttsSpeak(t); }});
        }
        if (i != null && "bird".equals(i.getStringExtra("panel"))) {
            ui.post(new Runnable() { public void run() { showBirdPanel(); }});
        } else if (i == null || (!i.hasExtra("tts"))) {
            ui.post(new Runnable() { public void run() { showPanel(); }});   // 常规拉起=喊话台
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        mode = M_IDLE;
        svcUp = false;
        inst = null;
        if (probeThread != null) probeThread.interrupt();
        ctrlPool.shutdownNow();
        upPool.shutdownNow();
        try { wm.removeView(panel); } catch (Exception ig) {}
        try { wm.removeView(bubble); } catch (Exception ig) {}
        try { wm.removeView(birdPanel); } catch (Exception ig) {}
        super.onDestroy();
    }

    // ================= 悬浮窗基础 =================

    LinearLayout birdPanel;
    WindowManager.LayoutParams birdLp;
    boolean birdUltraOn = false, birdStrobeOn = false;
    TextView birdUltraBtn, birdStrobeBtn, birdHintText, birdStatusText;
    View birdStatusFill;

    /** 驱鸟悬浮窗: 界面仿喊话台(渐变底/标题行/hint/状态条/mkBtn 渐变按钮两行) */
    void buildBirdPanel() {
        birdLp = baseLp("birdpanel", 24, 420);
        birdLp.width = dp(240);
        birdPanel = new LinearLayout(this);
        birdPanel.setOrientation(LinearLayout.VERTICAL);
        GradientDrawable bg = new GradientDrawable(GradientDrawable.Orientation.TL_BR,
                new int[]{0xF2101B33, 0xF2060B16});
        bg.setCornerRadius(dp(16));
        birdPanel.setBackground(bg);
        birdPanel.setPadding(dp(14), dp(4), dp(14), dp(12));
        dragHandler(birdPanel, birdPanel, birdLp, "birdpanel", null);

        // 标题行(同喊话台: 标题 + 状态灯 + ✕)
        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.HORIZONTAL);
        top.setGravity(Gravity.CENTER_VERTICAL);
        top.setPadding(0, dp(8), 0, dp(8));
        TextView tt = new TextView(this);
        tt.setText("🐦 驱鸟台");
        tt.setTextColor(0xFFF1F5F9);
        tt.setTextSize(15);
        tt.setTypeface(Typeface.DEFAULT_BOLD);
        top.addView(tt, new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        birdStatusText = new TextView(this);
        birdStatusText.setText("○");
        birdStatusText.setTextColor(0xFF64748B);
        birdStatusText.setTextSize(14);
        birdStatusText.setPadding(0, 0, dp(12), 0);
        top.addView(birdStatusText);
        TextView exit = mkGhost("✕");
        exit.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { closeBirdPanel(true); }
        });
        top.addView(exit);
        dragHandler(top, birdPanel, birdLp, "birdpanel", null);
        birdPanel.addView(top, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        // hint 状态行(同喊话台样式)
        birdHintText = new TextView(this);
        birdHintText.setText("超声波 关 · 爆闪 关");
        birdHintText.setTextColor(0xFF475569);
        birdHintText.setTextSize(11);
        birdHintText.setMinLines(2);
        birdHintText.setPadding(0, 0, 0, dp(4));
        birdPanel.addView(birdHintText, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        // 状态指示条(仿电平条: 开关数量映射宽度)
        FrameLayout track = new FrameLayout(this);
        track.setBackground(round(0xFF0B1426, 4));
        birdStatusFill = new View(this);
        GradientDrawable fg = new GradientDrawable(GradientDrawable.Orientation.LEFT_RIGHT,
                new int[]{0xFF22D3EE, 0xFF34D399});
        fg.setCornerRadius(dp(4));
        birdStatusFill.setBackground(fg);
        FrameLayout.LayoutParams fp = new FrameLayout.LayoutParams(
                dp(3), FrameLayout.LayoutParams.MATCH_PARENT);
        birdStatusFill.setLayoutParams(fp);
        track.addView(birdStatusFill);
        LinearLayout.LayoutParams ltp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(5));
        ltp.topMargin = dp(8);
        birdPanel.addView(track, ltp);

        // 按钮行1: 超声波 / 爆闪(同喊话台 mkBtn 渐变按钮, 开关态换色换字)
        LinearLayout br = new LinearLayout(this);
        br.setOrientation(LinearLayout.HORIZONTAL);
        birdUltraBtn = mkBtn("超声波驱鸟", new int[]{0xFF3B82F6, 0xFF1D4ED8});
        birdStrobeBtn = mkBtn("开启爆闪灯", new int[]{0xFFFB923C, 0xFFC2410C});
        birdUltraBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { sendBird("ULTRA", !birdUltraOn); }
        });
        birdStrobeBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { sendBird("STROBE", !birdStrobeOn); }
        });
        LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(0, dp(46), 1);
        bp.rightMargin = dp(8);
        br.addView(birdUltraBtn, bp);
        br.addView(birdStrobeBtn, new LinearLayout.LayoutParams(0, dp(46), 1));
        LinearLayout.LayoutParams brp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        brp.topMargin = dp(10);
        birdPanel.addView(br, brp);

        // 按钮行2: 全部关闭(灰) + 控制台(蓝, 同喊话台)
        LinearLayout br2 = new LinearLayout(this);
        br2.setOrientation(LinearLayout.HORIZONTAL);
        TextView alloff = mkBtn("全部关闭", new int[]{0xFF334155, 0xFF1E293B});
        alloff.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                if (!birdUltraOn && !birdStrobeOn) { toast("已全部关闭"); return; }
                java.util.ArrayList<String[]> cs = new java.util.ArrayList<String[]>();
                if (birdUltraOn) cs.add(new String[]{"ULTRA", "0"});
                if (birdStrobeOn) cs.add(new String[]{"STROBE", "0"});
                sendBirds(cs.toArray(new String[0][]));
            }
        });
        LinearLayout.LayoutParams lpL = new LinearLayout.LayoutParams(0, dp(46), 1);
        lpL.rightMargin = dp(8);
        br2.addView(alloff, lpL);
        TextView con = mkBtn("控制台", new int[]{0xFF3B82F6, 0xFF1D4ED8});
        con.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                closeBirdPanel(false);
                Intent it = new Intent(PaService.this, PaSend.class);
                it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(it);
            }
        });
        br2.addView(con, new LinearLayout.LayoutParams(0, dp(46), 1));
        LinearLayout.LayoutParams lp2 = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp2.topMargin = dp(8);
        birdPanel.addView(br2, lp2);

        refreshBirdPanel();
    }

    /** 关鸟面板; 喊话面板也不在了才收服务 */
    void closeBirdPanel(boolean toastIt) {
        try { wm.removeView(birdPanel); } catch (Exception ig) {}
        if (panel.getParent() == null && bubble.getParent() == null) {
            mode = M_IDLE;
            stopForeground(true);
            stopSelf();
        }
        if (toastIt) toast("驱鸟台已关闭");
    }

    void showBirdPanel() {
        if (birdPanel == null) buildBirdPanel();
        if (birdPanel.getParent() == null) wm.addView(birdPanel, birdLp);
    }

    /** 单开关: ULTRA/STROBE 开或关 */
    void sendBird(String sub, boolean on) {
        sendBirds(new String[][]{{sub, on ? "1" : "0"}});
    }

    /** BIRD 指令串行执行(cmds={{子命令,0|1},..}), 全部完成后回 UI 线程
     *  统一刷新+合并 toast(并发改状态会竞态, 且失败时 toast 叠弹) */
    void sendBirds(final String[][] cmds) {
        ctrlExec(new Runnable() {
            public void run() {
                final java.util.ArrayList<String[]> results = new java.util.ArrayList<String[]>();
                for (String[] c : cmds) {
                    String resp = "";
                    DatagramSocket s = null;
                    try {
                        s = new DatagramSocket();
                        s.connect(InetAddress.getByName(DST_IP), CTRL_PORT);
                        byte[] ob = ("BIRD " + c[0] + " " + c[1]).getBytes();
                        s.send(new DatagramPacket(ob, ob.length));
                        resp = waitAck(s, 1500);
                    } catch (Exception e) { }
                    finally {
                        try { if (s != null) s.close(); } catch (Exception ig) {}
                    }
                    results.add(new String[]{c[0], c[1], resp.startsWith("OK") ? "1" : "0"});
                }
                ui.post(new Runnable() {
                    public void run() {
                        boolean anyFail = false;
                        for (String[] r : results) {
                            if (r[2].equals("1")) {
                                if (r[0].equals("ULTRA")) birdUltraOn = r[1].equals("1");
                                else birdStrobeOn = r[1].equals("1");
                            } else {
                                anyFail = true;
                            }
                        }
                        if (anyFail) toast("❌ 板子无响应");
                        refreshBirdPanel();
                    }
                });
            }
        });
    }

    /** 刷新鸟面板: 按钮文字/配色, hint, 状态条宽度 */
    void refreshBirdPanel() {
        if (birdPanel == null) return;
        int n = (birdUltraOn ? 1 : 0) + (birdStrobeOn ? 1 : 0);
        birdUltraBtn.setText(birdUltraOn ? "关闭超声波" : "超声波驱鸟");
        birdUltraBtn.setBackground(grad(birdUltraOn
                ? new int[]{0xFF10B981, 0xFF047857} : new int[]{0xFF3B82F6, 0xFF1D4ED8}, 10));
        birdStrobeBtn.setText(birdStrobeOn ? "关闭爆闪灯" : "开启爆闪灯");
        birdStrobeBtn.setBackground(grad(birdStrobeOn
                ? new int[]{0xFFFBBF24, 0xFFD97706} : new int[]{0xFFFB923C, 0xFFC2410C}, 10));
        birdHintText.setText("超声波 " + (birdUltraOn ? "开" : "关")
                + " · 爆闪 " + (birdStrobeOn ? "开" : "关"));
        birdStatusText.setText(n > 0 ? "●" : "○");
        birdStatusText.setTextColor(n > 0 ? 0xFF34D399 : 0xFF64748B);
        FrameLayout.LayoutParams fp = (FrameLayout.LayoutParams) birdStatusFill.getLayoutParams();
        fp.width = n == 0 ? dp(3) : n == 1 ? dp(107) : dp(212);
        birdStatusFill.setLayoutParams(fp);
    }

    int dp(float v) {
        return Math.round(getResources().getDisplayMetrics().density * v);
    }

    GradientDrawable round(int color, float r) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(color);
        g.setCornerRadius(dp(r));
        return g;
    }

    GradientDrawable grad(int[] colors, float r) {
        GradientDrawable g = new GradientDrawable(GradientDrawable.Orientation.TL_BR, colors);
        g.setCornerRadius(dp(r));
        return g;
    }

    int overlayType() {
        return Build.VERSION.SDK_INT >= 26
                ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                : WindowManager.LayoutParams.TYPE_PHONE;
    }

    WindowManager.LayoutParams baseLp(String key, int defX, int defY) {
        WindowManager.LayoutParams p = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.WRAP_CONTENT,
                overlayType(),
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
                PixelFormat.TRANSLUCENT);
        p.gravity = Gravity.TOP | Gravity.LEFT;
        p.x = prefs.getInt(key + "_x", dp(defX));
        p.y = prefs.getInt(key + "_y", dp(defY));
        return p;
    }

    void showPanel() {
        expanded = true;
        try { wm.removeView(bubble); } catch (Exception ig) {}
        if (panel.getParent() == null) wm.addView(panel, panelLp);
        renderMode();
    }

    void showBubble() {
        expanded = false;
        try { wm.removeView(panel); } catch (Exception ig) {}
        if (bubble.getParent() == null) wm.addView(bubble, bubbleLp);
    }

    /**
     * 拖动+点击两用: DOWN 必须消费(return true)后续 MOVE 才会派发(v5 return false 拖不动);
     * 移动超系统 slop 才算拖, 原地松手算点击(否则 onClick 永远不触发, v6~v8 圆钮点不开的根因)
     * 注意: updateViewLayout 只能打在窗口根 root 上——它会先 setLayoutParams(WM参数)再查挂载,
     * 打在子 View(标题行)上会把窗口参数种进子 View, 下一次测量 LinearLayout 转型直接闪退(v28 实测)
     */
    void dragHandler(final View v, final View root, final WindowManager.LayoutParams lp,
                     final String key, final Runnable onClick) {
        final int slop = android.view.ViewConfiguration.get(this).getScaledTouchSlop();
        v.setOnTouchListener(new View.OnTouchListener() {
            float dx, dy;
            int downX, downY;
            boolean moved;
            public boolean onTouch(View view, MotionEvent e) {
                switch (e.getAction()) {
                    case MotionEvent.ACTION_DOWN:
                        dx = lp.x - e.getRawX();
                        dy = lp.y - e.getRawY();
                        downX = (int) e.getRawX();
                        downY = (int) e.getRawY();
                        moved = false;
                        return true;
                    case MotionEvent.ACTION_MOVE:
                        if (!moved && (Math.abs(e.getRawX() - downX) > slop
                                || Math.abs(e.getRawY() - downY) > slop)) moved = true;
                        if (moved) {
                            lp.x = Math.round(dx + e.getRawX());
                            lp.y = Math.round(dy + e.getRawY());
                            try {
                                wm.updateViewLayout(root, lp);   // 永远更新窗口根
                            } catch (Exception ig) {}
                        }
                        return true;
                    case MotionEvent.ACTION_UP:
                    case MotionEvent.ACTION_CANCEL:
                        prefs.edit().putInt(key + "_x", lp.x)
                                .putInt(key + "_y", lp.y).apply();
                        if (!moved && onClick != null) onClick.run();
                        return true;
                }
                return false;
            }
        });
    }

    // ================= 面板构建 =================

    void buildBubble() {
        bubbleLp = baseLp("bubble", 8, 160);
        bubbleLp.width = dp(100);          // 两倍大圆钮
        bubbleLp.height = dp(100);
        bubble = new TextView(this);
        bubble.setText("🎙");
        bubble.setTextSize(56);
        bubble.setGravity(Gravity.CENTER);
        bubble.setBackground(grad(new int[]{0xCC3B82F6, 0xCC1D4ED8}, 999));
        dragHandler(bubble, bubble, bubbleLp, "bubble", new Runnable() {
            public void run() { showPanel(); }
        });
    }

    void buildPanel() {
        panelLp = baseLp("panel", 24, 80);
        panelLp.width = dp(240);          // 固定宽: 文字变长/拖动时尺寸不再跳
        panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        GradientDrawable bg = new GradientDrawable(GradientDrawable.Orientation.TL_BR,
                new int[]{0xF2101B33, 0xF2060B16});
        bg.setCornerRadius(dp(16));
        panel.setBackground(bg);
        panel.setPadding(dp(14), dp(4), dp(14), dp(12));
        dragHandler(panel, panel, panelLp, "panel", null);   // 空白处也可拖

        // 标题行(拖动区, 加高好抓)
        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.HORIZONTAL);
        top.setGravity(Gravity.CENTER_VERTICAL);
        top.setPadding(0, dp(8), 0, dp(8));
        TextView tt = new TextView(this);
        tt.setText("喊话台");
        tt.setTextColor(0xFFF1F5F9);
        tt.setTextSize(15);
        tt.setTypeface(Typeface.DEFAULT_BOLD);
        top.addView(tt, new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        statusText = new TextView(this);
        statusText.setText("○");
        statusText.setTextColor(0xFF64748B);
        statusText.setTextSize(14);
        statusText.setPadding(0, 0, dp(12), 0);
        top.addView(statusText);
        TextView hide = mkGhost("—");
        hide.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { showBubble(); }
        });
        top.addView(hide);
        TextView exit = mkGhost("✕");
        exit.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                closePaPanels();
                toast("喊话台已关闭");
            }
        });
        top.addView(exit);
        dragHandler(top, panel, panelLp, "panel", null);
        panel.addView(top, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        hint = new TextView(this);
        hint.setText("未在喊话");
        hint.setTextColor(0xFF475569);
        hint.setTextSize(11);
        hint.setMinLines(2);
        hint.setPadding(0, 0, 0, dp(4));
        panel.addView(hint, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        // 音量行: [-] 45% [+]
        LinearLayout vr = new LinearLayout(this);
        vr.setOrientation(LinearLayout.HORIZONTAL);
        vr.setGravity(Gravity.CENTER_VERTICAL);
        TextView vm = mkVolBtn("−");
        vm.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { setVol(curVol - 5); }
        });
        vr.addView(vm, new LinearLayout.LayoutParams(dp(32), dp(28)));
        volText = new TextView(this);
        volText.setText(curVol + "%");
        volText.setTextColor(0xFFE2E8F0);
        volText.setTextSize(22);
        volText.setTypeface(Typeface.DEFAULT_BOLD);
        volText.setGravity(Gravity.CENTER);
        vr.addView(volText, new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.MATCH_PARENT, 1));
        TextView vp = mkVolBtn("+");
        vp.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { setVol(curVol + 5); }
        });
        vr.addView(vp, new LinearLayout.LayoutParams(dp(32), dp(28)));
        panel.addView(vr, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        // 电平条
        levelTrack = new FrameLayout(this);
        levelTrack.setBackground(round(0xFF0B1426, 4));
        levelFill = new View(this);
        GradientDrawable fg = new GradientDrawable(GradientDrawable.Orientation.LEFT_RIGHT,
                new int[]{0xFF22D3EE, 0xFF34D399});
        fg.setCornerRadius(dp(4));
        levelFill.setBackground(fg);
        FrameLayout.LayoutParams fp = new FrameLayout.LayoutParams(
                dp(3), FrameLayout.LayoutParams.MATCH_PARENT);
        levelFill.setLayoutParams(fp);
        levelTrack.addView(levelFill);
        LinearLayout.LayoutParams ltp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(5));
        ltp.topMargin = dp(8);
        panel.addView(levelTrack, ltp);

        // 按钮行1: 喊话 / 录音(间距在容器上, 两按钮完全一致才对得齐)
        LinearLayout br = new LinearLayout(this);
        br.setOrientation(LinearLayout.HORIZONTAL);
        btnTalk = mkBtn("开始喊话", new int[]{0xFF3B82F6, 0xFF1D4ED8});
        btnRec = mkBtn("开始录音", new int[]{0xFFFB923C, 0xFFC2410C});
        btnTalk.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                if (mode == M_IDLE) startLive();
                else setMode(M_IDLE);
            }
        });
        btnRec.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                if (mode == M_IDLE) startRec();
                else if (mode == M_REC) setMode(M_IDLE);
            }
        });
        LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(0, dp(46), 1);
        bp.rightMargin = dp(8);
        br.addView(btnTalk, bp);
        br.addView(btnRec, new LinearLayout.LayoutParams(0, dp(46), 1));
        LinearLayout.LayoutParams brp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        brp.topMargin = dp(10);
        panel.addView(br, brp);

        // 按钮行2: 录音库(半宽) + 控制台(半宽, 拉回主界面并收悬浮窗)
        LinearLayout br2 = new LinearLayout(this);
        br2.setOrientation(LinearLayout.HORIZONTAL);
        TextView lib = mkBtn("录音库", new int[]{0xFF334155, 0xFF1E293B});
        lib.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { showLibrary(); }
        });
        LinearLayout.LayoutParams lpL = new LinearLayout.LayoutParams(0, dp(46), 1);
        lpL.rightMargin = dp(8);
        br2.addView(lib, lpL);
        TextView con = mkBtn("控制台", new int[]{0xFF3B82F6, 0xFF1D4ED8});
        con.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { openConsole(); }
        });
        br2.addView(con, new LinearLayout.LayoutParams(0, dp(46), 1));
        LinearLayout.LayoutParams lp2 = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp2.topMargin = dp(8);
        panel.addView(br2, lp2);

        // 快捷芯片排
        HorizontalScrollView hs = new HorizontalScrollView(this);
        hs.setHorizontalScrollBarEnabled(false);
        chipsRow = new LinearLayout(this);
        chipsRow.setOrientation(LinearLayout.HORIZONTAL);
        chipsRow.setPadding(0, dp(8), 0, 0);
        hs.addView(chipsRow, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        panel.addView(hs, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        buildChips();

        // 底行: 左=话质切换(16k高清/8k省带宽) 右=检查更新(小字)
        LinearLayout fr2 = new LinearLayout(this);
        fr2.setOrientation(LinearLayout.HORIZONTAL);
        fr2.setGravity(Gravity.CENTER_VERTICAL);
        rateBtn = new TextView(this);
        rateBtn.setTextSize(12);
        rateBtn.setPadding(dp(2), dp(8), dp(6), dp(2));
        rateBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { toggleTalkRate(); }
        });
        updateRateBtn();
        fr2.addView(rateBtn, new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        updBtn = new TextView(this);
        updBtn.setText("检查更新");
        updBtn.setTextColor(0xFF64748B);
        updBtn.setTextSize(12);
        updBtn.setPadding(dp(6), dp(8), dp(2), dp(2));
        fr2.addView(updBtn);
        updBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { onUpdateClicked(); }
        });
        panel.addView(fr2, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
    }

    /** 收喊话面板+bubble; 若驱鸟台还挂着只收自己(服务保留), 全空才停服务 */
    void closePaPanels() {
        try { wm.removeView(panel); } catch (Exception ig) {}
        try { wm.removeView(bubble); } catch (Exception ig) {}
        boolean birdUp = false;
        try { birdUp = birdPanel != null && birdPanel.getParent() != null; } catch (Exception ig) {}
        if (!birdUp) {
            mode = M_IDLE;
            stopForeground(true);
            stopSelf();
        }
    }

    /** 控制台按钮: 拉回主界面(WebView 调试台)并收掉悬浮窗 */
    void openConsole() {
        try {
            Intent i = new Intent(this, PaSend.class);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(i);
        } catch (Exception e) {
            toast("打开控制台失败");
            return;
        }
        closePaPanels();
    }

    TextView mkGhost(String s) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextColor(0xFF94A3B8);
        t.setTextSize(16);
        t.setTypeface(Typeface.DEFAULT_BOLD);
        t.setPadding(dp(10), dp(6), dp(2), dp(6));
        return t;
    }

    TextView mkVolBtn(String s) {
        TextView b = new TextView(this);
        b.setText(s);
        b.setTextColor(0xFFFFFFFF);
        b.setTextSize(16);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setGravity(Gravity.CENTER);
        b.setBackground(grad(new int[]{0xFF3B82F6, 0xFF1D4ED8}, 10));
        return b;
    }

    TextView mkBtn(String text, int[] colors) {
        TextView b = new TextView(this);
        b.setText(text);
        b.setTextColor(0xFFFFFFFF);
        b.setTextSize(15);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setGravity(Gravity.CENTER);
        b.setBackground(grad(colors, 10));
        return b;
    }

    void buildChips() {
        if (chipsRow == null) return;
        chipsRow.removeAllViews();
        for (int i = 0; i < presets.length(); i++) {
            JSONObject o = presets.optJSONObject(i);
            if (o == null) continue;
            final int idx = i;
            boolean act = (mode == M_PLAY || mode == M_AUD)
                    && o.optString("name").equals(playingName);
            TextView chip = new TextView(this);
            chip.setText((act ? "▶ " : "") + o.optString("name"));
            chip.setTextColor(act ? 0xFF6EE7B7 : 0xFFE2E8F0);
            chip.setTextSize(13);
            chip.setPadding(dp(14), dp(7), dp(14), dp(7));
            chip.setBackground(round(act ? 0xFF065F46 : 0xFF1E293B, 18));
            chip.setOnClickListener(new View.OnClickListener() {
                public void onClick(View v) { playPreset(idx); }
            });
            LinearLayout.LayoutParams cp = new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
            cp.rightMargin = dp(6);
            chipsRow.addView(chip, cp);
        }
    }

    // ================= 模式状态机 =================

    void setMode(int m) {
        mode = m;
        ui.post(new Runnable() { public void run() { renderMode(); }});
    }

    void renderMode() {
        if (!expanded) return;
        if (mode == M_LIVE) {
            btnTalk.setText("停止喊话");
            btnTalk.setBackground(grad(new int[]{0xFFF87171, 0xFFB91C1C}, 10));
            hint.setText("● 喊话中… 对麦克风说话");
            hint.setTextColor(0xFFF87171);
        } else if (mode == M_REC) {
            btnRec.setText("完成录音");
            hint.setText("● 录音中… 点「完成录音」保存(最长60秒)");
            hint.setTextColor(0xFFFB923C);
        } else if (mode == M_PLAY) {
            hint.setText("▶ 板子播放中: " + playingName + " (再点一次停止)");
            hint.setTextColor(0xFF34D399);
        } else if (mode == M_AUD) {
            hint.setText("♪ 试听中: " + playingName + " (再点一次停止)");
            hint.setTextColor(0xFF22D3EE);
        } else {
            btnTalk.setText("开始喊话");
            btnTalk.setBackground(grad(new int[]{0xFF3B82F6, 0xFF1D4ED8}, 10));
            btnRec.setText("开始录音");
            hint.setText("○ 未在喊话");
            hint.setTextColor(0xFF475569);
            setLevel(0);
        }
        buildChips();   // 刷新播放高亮
    }

    void setLevel(int level) {
        int base = levelTrack.getWidth();       // 按容器实际宽度, 防溢出
        if (base <= 0) base = dp(172);
        FrameLayout.LayoutParams p = (FrameLayout.LayoutParams) levelFill.getLayoutParams();
        p.width = Math.max(dp(3), base * level / 100);
        levelFill.setLayoutParams(p);
    }

    void toast(final String s) {
        ui.post(new Runnable() { public void run() {
            Toast.makeText(PaService.this, s, Toast.LENGTH_SHORT).show();
        }});
    }

    // ================= 实时喊话 =================

    void startLive() {
        if (checkMic()) return;
        if (mode == M_IDLE) { startLiveNow(); return; }
        setMode(M_IDLE);
        ui.postDelayed(new Runnable() { public void run() { startLiveNow(); } }, 200);
    }

    void startLiveNow() {
        try {      // 低延迟 WiFi 锁: 省电模式会让 UDP 周期性整窗丢包(实测恒定丢~15%)
            android.net.wifi.WifiManager wmgr = (android.net.wifi.WifiManager)
                    getApplicationContext().getSystemService(WIFI_SERVICE);
            if (wifiLock == null) {
                wifiLock = wmgr.createWifiLock(
                        android.net.wifi.WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "pa");
                wifiLock.setReferenceCounted(false);
            }
            wifiLock.acquire();
        } catch (Exception ig) {}
        setMode(M_LIVE);
        workThread = new Thread() { public void run() { liveLoop(); } };
        workThread.start();
    }

    boolean checkMic() {
        if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            toast("先到喊话台主界面授予麦克风权限");
            return true;
        }
        return false;
    }

    void liveLoop() {
        android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_URGENT_AUDIO);
        AudioRecord rec = null;
        DatagramSocket sock = null;
        boolean wasLive = false;
        try {
            int min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT);
            rec = new AudioRecord(MediaRecorder.AudioSource.MIC, RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                    Math.max(min, RATE * 2));
            if (rec.getState() != AudioRecord.STATE_INITIALIZED)
                throw new IllegalStateException("AudioRecord init failed");
            rec.startRecording();
            sock = new DatagramSocket();
            InetAddress dst = InetAddress.getByName(DST_IP);
            ctrl("RATE " + talkRate);              // 话质(采样率)在开播前切换
            String tr = null;                      // 15% 链路丢包下单发指令也可能丢, 重试3次
            for (int t = 0; t < 3; t++) {
                tr = ctrl("TALK 1");
                if (tr != null && tr.startsWith("OK")) break;
                try { Thread.sleep(150); } catch (InterruptedException ig) { return; }
            }
            if (tr == null || !tr.startsWith("OK")) {
                ui.post(new Runnable() { public void run() {
                    toast("板子喊话通道未就绪(看探活灯)");
                }});
                return;                            // finally 会收尾并复位
            }
            wasLive = true;
            liveSent = 0;
            short[] pcm = new short[FRAME];
            byte[] out = new byte[FRAME * 2];
            long t0 = System.nanoTime(), sentSmp = 0;
            while (mode == M_LIVE) {
                int n = rec.read(pcm, 0, FRAME);
                if (n <= 0) continue;
                long acc = 0;
                int sentN;
                if (talkRate == 8000) {
                    // 真降采样 16k->8k(相邻取均值): 只切板端播放率不减样本的话, 8k 放 16k 数据 = 怪兽音
                    int m = n / 2;
                    for (int i = 0; i < m; i++) {
                        int s = ((pcm[2 * i] + pcm[2 * i + 1]) / 2) * LIVE_BOOST;
                        if (s > 32767) s = 32767;
                        else if (s < -32768) s = -32768;
                        out[i * 2] = (byte) (s & 0xFF);
                        out[i * 2 + 1] = (byte) ((s >> 8) & 0xFF);
                        acc += (long) s * s;
                    }
                    sentN = m * 2;
                    postLevel(acc, Math.max(1, m));
                } else {
                    for (int i = 0; i < n; i++) {
                        int s = pcm[i] * LIVE_BOOST;
                        if (s > 32767) s = 32767;
                        else if (s < -32768) s = -32768;
                        out[i * 2] = (byte) (s & 0xFF);
                        out[i * 2 + 1] = (byte) ((s >> 8) & 0xFF);
                        acc += (long) s * s;
                    }
                    sentN = n * 2;
                    postLevel(acc, n);
                }
                sock.send(new DatagramPacket(out, sentN, dst, AUDIO_PORT));
                liveSent++;
                // 匀速发送: 麦克风读取常成簇返回, 突发包会被无线链路队列挤掉
                sentSmp += n;
                long want = sentSmp * 1000000000L / RATE;
                long cur = System.nanoTime() - t0;
                if (cur + 4000000 < want) {
                    try { Thread.sleep(Math.min(40, (want - cur - 4000000) / 1000000L)); }
                    catch (InterruptedException ig) { break; }
                }
            }
        } catch (Exception e) {
            if (mode == M_LIVE) toast("喊话出错: " + e.getMessage());
        } finally {
            if (rec != null) {
                try { rec.stop(); } catch (Exception ig) {}
                rec.release();
            }
            if (sock != null) sock.close();
            try {
                if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
            } catch (Exception ig) {}
            if (wasLive) {
                String st = ctrl("TALK 0 " + liveSent);   // 报发包数, 板端回丢包率
                if (st != null && st.startsWith("STAT")) {
                    addLog("对讲丢包 " + st.substring(5));
                    ui.post(new Runnable() { public void run() {
                        toast("本轮对讲 " + st.substring(5)
                                .replace("recv=", "收").replace("sent=", "发")
                                .replace("loss=", "丢包"));
                    }});
                } else {
                    addLog("对讲结束, 板子无应答");
                }
            }
            if (mode == M_LIVE) setMode(M_IDLE);
        }
    }

    void postLevel(long acc, int n) {
        final int level = (int) Math.min(100,
                Math.sqrt(acc / (double) Math.max(1, n)) / 32768.0 * 400);
        ui.post(new Runnable() { public void run() { setLevel(level); }});
    }

    // ================= 录音(60s 上限) =================

    void startRec() {
        if (checkMic()) return;
        if (mode == M_IDLE) { startRecNow(); return; }
        setMode(M_IDLE);
        ui.postDelayed(new Runnable() { public void run() { startRecNow(); } }, 200);
    }

    void startRecNow() {
        setMode(M_REC);
        final String name = "录音_" + new SimpleDateFormat("HHmmss",
                Locale.CHINA).format(new Date());
        workThread = new Thread() { public void run() { recLoop(name); } };
        workThread.start();
    }

    void recLoop(String name) {
        AudioRecord rec = null;
        FileOutputStream fo = null;
        File tmp = new File(getFilesDir(), "rec_tmp.pcm");
        long bytes = 0;
        final boolean[] hitMax = {false};
        try {
            int min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT);
            rec = new AudioRecord(MediaRecorder.AudioSource.MIC, RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                    Math.max(min, RATE * 2));
            if (rec.getState() != AudioRecord.STATE_INITIALIZED)
                throw new IllegalStateException("AudioRecord init failed");
            rec.startRecording();
            fo = new FileOutputStream(tmp);
            short[] pcm = new short[FRAME];
            byte[] out = new byte[FRAME * 2];
            while (mode == M_REC) {
                if (bytes >= REC_MAX_BYTES) { hitMax[0] = true; break; }
                int n = rec.read(pcm, 0, FRAME);
                if (n <= 0) continue;
                long acc = 0;
                for (int i = 0; i < n; i++) {
                    short s = pcm[i];
                    out[i * 2] = (byte) (s & 0xFF);
                    out[i * 2 + 1] = (byte) ((s >> 8) & 0xFF);
                    acc += (long) s * s;
                }
                fo.write(out, 0, n * 2);
                bytes += n * 2;
                postLevel(acc, n);
            }
        } catch (Exception e) {
            if (mode == M_REC) toast("录音出错: " + e.getMessage());
            return;
        } finally {
            if (rec != null) {
                try { rec.stop(); } catch (Exception ig) {}
                rec.release();
            }
            try { if (fo != null) fo.close(); } catch (Exception ig) {}
        }
        if (bytes < 3200) {
            tmp.delete();
            ui.post(new Runnable() { public void run() {
                setMode(M_IDLE); toast("太短了, 没保存");
            }});
            return;
        }
        final String file = "preset_" + System.currentTimeMillis() + ".pcm";
        new File(getFilesDir(), file).delete();
        tmp.renameTo(new File(getFilesDir(), file));
        addPresetEntry(name, file, bytes / 32000.0);
        enqueueUpload(name, false);
        ui.post(new Runnable() { public void run() {
            setMode(M_IDLE);
            toast(hitMax[0] ? "已到 60 秒自动停止, 已存入录音库" : "已存入录音库: " + name);
        }});
    }

    // ================= 录音库对话框 =================

    void showLibrary() {
        if (libDialog != null) {                 // 防对话框叠加(删除/导入后刷新重开)
            try { libDialog.dismiss(); } catch (Exception ig) {}
            libDialog = null;
        }
        ScrollView sc = new ScrollView(this);
        LinearLayout list = new LinearLayout(this);
        list.setOrientation(LinearLayout.VERTICAL);
        list.setPadding(dp(4), dp(4), dp(4), dp(4));
        sc.addView(list);

        for (int i = 0; i < presets.length(); i++) {
            JSONObject o = presets.optJSONObject(i);
            if (o == null) continue;
            final int idx = i;
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.setGravity(Gravity.CENTER_VERTICAL);
            row.setPadding(dp(10), dp(10), dp(10), dp(10));
            row.setBackground(round(0xFF1E293B, 10));
            LinearLayout.LayoutParams rp = new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
            rp.bottomMargin = dp(6);
            list.addView(row, rp);

            TextView name = new TextView(this);
            int bst = o.optInt("bst", 0);
            boolean isUp = uploading.contains(o.optString("name"));
            String st = isUp ? " ↑同步中…"
                    : bst == 1 ? " ✓" : bst == 2 ? " ⚠不同步" : " ↑未同步";
            name.setText(o.optString("name") + " (" + o.optDouble("dur", 0) + "s" + st + ")");
            name.setTextColor(isUp ? 0xFFFBBF24 : bst == 1 ? 0xFFE2E8F0 : 0xFFF87171);
            name.setTextSize(14);
            row.addView(name, new LinearLayout.LayoutParams(0,
                    LinearLayout.LayoutParams.WRAP_CONTENT, 1));
            row.setOnClickListener(new View.OnClickListener() {
                public void onClick(View v) { playPreset(idx); }
            });
            if (!o.optString("file").isEmpty()) {
                TextView up = new TextView(this);
                up.setText("上传");
                up.setTextColor(0xFF60A5FA);
                up.setTextSize(14);
                up.setPadding(dp(12), dp(8), dp(6), dp(8));
                row.addView(up);
                up.setOnClickListener(new View.OnClickListener() {
                    public void onClick(View v) { enqueueUpload(o.optString("name"), false); }
                });
            }
            if (!o.optString("file").isEmpty()) {      // 板上补录的条目没有本地文件, 不能试听
                TextView aud = new TextView(this);
                aud.setText("试听");
                aud.setTextColor(0xFF34D399);
                aud.setTextSize(14);
                aud.setPadding(dp(12), dp(8), dp(6), dp(8));
                row.addView(aud);
                aud.setOnClickListener(new View.OnClickListener() {
                    public void onClick(View v) { toggleAud(idx); }
                });
            }
            TextView rn = new TextView(this);
            rn.setText("改名");
            rn.setTextColor(0xFF60A5FA);
            rn.setTextSize(14);
            rn.setPadding(dp(8), dp(8), dp(6), dp(8));
            row.addView(rn);
            rn.setOnClickListener(new View.OnClickListener() {
                public void onClick(View v) { renamePreset(idx); }
            });
            TextView del = new TextView(this);
            del.setText("删除");
            del.setTextColor(0xFFF87171);
            del.setTextSize(14);
            del.setPadding(dp(6), dp(8), dp(2), dp(8));
            row.addView(del);
            del.setOnClickListener(new View.OnClickListener() {
                public void onClick(View v) {
                    if (mode == M_PLAY || mode == M_AUD) setMode(M_IDLE);
                    deletePreset(idx);
                }
            });
        }

        TextView add = new TextView(this);
        add.setText("＋ 从文件添加 (mp3 / m4a / wav)");
        add.setTextColor(0xFF60A5FA);
        add.setTextSize(13);
        add.setGravity(Gravity.CENTER);
        add.setPadding(dp(6), dp(12), dp(6), dp(12));
        GradientDrawable dg = round(0x00000000, 10);
        dg.setStroke(dp(1), 0xFF334155);
        add.setBackground(dg);
        list.addView(add, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        add.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                Intent i = new Intent(PaService.this, ImportActivity.class);
                i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(i);
            }
        });

        TextView scan = new TextView(this);
        scan.setText("📂 扫描 Download 目录");
        scan.setTextColor(0xFF34D399);
        scan.setTextSize(13);
        scan.setGravity(Gravity.CENTER);
        scan.setPadding(dp(6), dp(12), dp(6), dp(12));
        GradientDrawable dg2 = round(0x00000000, 10);
        dg2.setStroke(dp(1), 0xFF334155);
        scan.setBackground(dg2);
        LinearLayout.LayoutParams scp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        scp.topMargin = dp(6);
        list.addView(scan, scp);
        scan.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { scanDownload(); }
        });

        TextView ttsBtn = new TextView(this);
        ttsBtn.setText("✍ 文字转语音");
        ttsBtn.setTextColor(0xFFFBBF24);
        ttsBtn.setTextSize(13);
        ttsBtn.setGravity(Gravity.CENTER);
        ttsBtn.setPadding(dp(6), dp(12), dp(6), dp(12));
        GradientDrawable dg3 = round(0x00000000, 10);
        dg3.setStroke(dp(1), 0xFF334155);
        ttsBtn.setBackground(dg3);
        LinearLayout.LayoutParams ttp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        ttp.topMargin = dp(6);
        list.addView(ttsBtn, ttp);
        ttsBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { ttsDialog(); }
        });

        TextView logBtn = new TextView(this);
        logBtn.setText("📜 传输日志 (对讲丢包/上传)");
        logBtn.setTextColor(0xFF94A3B8);
        logBtn.setTextSize(13);
        logBtn.setGravity(Gravity.CENTER);
        logBtn.setPadding(dp(6), dp(12), dp(6), dp(12));
        GradientDrawable dg4 = round(0x00000000, 10);
        dg4.setStroke(dp(1), 0xFF334155);
        logBtn.setBackground(dg4);
        LinearLayout.LayoutParams lgb = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lgb.topMargin = dp(6);
        list.addView(logBtn, lgb);
        logBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { showLogs(); }
        });

        AlertDialog d = new AlertDialog.Builder(this)
                .setTitle("录音库 (点条目=同步到板子并播放, ✓=板上已有)")
                .setView(sc)
                .setNegativeButton("关闭", null)
                .create();
        d.getWindow().setType(overlayType());
        libDialog = d;
        d.show();
        refreshUpdBtn();
    }

    AlertDialog libDialog;

    void refreshUpdBtn() {
        if (updBtn == null) return;
        if (latestVer > APP_VERSION) {
            updBtn.setText("发现新版 v" + latestVer + " · 点击更新");
            updBtn.setTextColor(0xFF34D399);
            updBtn.setTypeface(Typeface.DEFAULT_BOLD);
        } else {
            updBtn.setText("检查更新");
            updBtn.setTextColor(0xFF64748B);
            updBtn.setTypeface(Typeface.DEFAULT);
        }
    }

    // ================= 预设库 =================

    void loadPresets() {
        try {
            presets = new JSONArray(prefs.getString("presets", "[]"));
        } catch (Exception e) {
            presets = new JSONArray();
        }
    }

    void savePresets() {
        prefs.edit().putString("presets", presets.toString()).apply();
    }

    void addPresetEntry(String name, String file, double dur) {
        try {
            JSONObject o = new JSONObject();
            o.put("name", sanitizeName(name));
            o.put("file", file);
            o.put("dur", Math.round(dur * 10) / 10.0);
            presets.put(o);
            savePresets();
            ui.post(new Runnable() { public void run() { buildChips(); }});
        } catch (Exception ig) {}
    }

    /** 让对话框可聚焦输入(Service overlay 窗口默认 NOT_FOCUSABLE, 里面的 EditText 打不了字) */
    void focusable(AlertDialog d) {
        android.view.Window w = d.getWindow();
        if (w == null) return;
        w.setType(overlayType());
        w.clearFlags(android.view.WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE);
        w.setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_STATE_VISIBLE);
    }

    void renamePreset(final int idx) {
        final JSONObject o = presets.optJSONObject(idx);
        if (o == null) return;
        final EditText et = new EditText(this);
        et.setText(o.optString("name"));
        et.setSelection(et.getText().length());
        AlertDialog d = new AlertDialog.Builder(this)
                .setTitle("重命名")
                .setView(et)
                .setPositiveButton("保存", new android.content.DialogInterface.OnClickListener() {
                    public void onClick(android.content.DialogInterface dg, int w) {
                        final String nn = sanitizeName(et.getText().toString());
                        if (nn.length() == 0) { toast("名字不能为空"); return; }
                        final String old = o.optString("name");
                        try {
                            presets.getJSONObject(idx).put("name", nn);
                            savePresets();
                        } catch (Exception ig) {}
                        buildChips();
                        showLibrary();
                        ctrlExec(new Runnable() { public void run() {
                            String r = ctrl("REN " + old + " " + nn);
                            addLog("改名 " + old + " -> " + nn + ": " + r);
                        }});
                    }
                })
                .setNegativeButton("取消", null)
                .create();
        focusable(d);
        d.show();
    }

    void deletePreset(int idx) {
        try {
            JSONObject o = presets.getJSONObject(idx);
            final String nm = o.optString("name");
            new File(getFilesDir(), o.optString("file")).delete();
            JSONArray np = new JSONArray();
            for (int i = 0; i < presets.length(); i++)
                if (i != idx) np.put(presets.getJSONObject(i));
            presets = np;
            savePresets();
            ctrlExec(new Runnable() { public void run() {
                String r = ctrl("DEL " + nm);
                addLog("删除 " + nm + ": " + r);
            }});
            toast("已删除");
            ui.post(new Runnable() { public void run() {
                buildChips();
                showLibrary();   // 重开对话框刷新列表
            }});
        } catch (Exception ig) {}
    }

    // ================= 预设播放(板端文件播放, 不再流式发音频) =================

    void playPreset(int idx) {
        if (idx < 0 || idx >= presets.length()) return;
        final JSONObject o = presets.optJSONObject(idx);
        if (o == null) return;
        final String name = o.optString("name");
        if (mode == M_PLAY && name.equals(playingName)) {      // 再点一次 = 停止
            stopBoardPlay();
            setMode(M_IDLE);
            return;
        }
        playingName = name;
        final int bst = o.optInt("bst", 0);
        if (bst != 1) {                    // 板上没有/crc 不符: 先完整上传, 板端校验通过才播
            toast(bst == 2 ? "板上校验不符, 重新同步…" : "先同步到板子…");
            enqueueUpload(name, true);
            if (mode != M_IDLE) setMode(M_IDLE);
            return;
        }
        if (mode == M_IDLE) { boardPlay(name, o.optDouble("dur", 0)); return; }
        final boolean wasLive = (mode == M_LIVE);              // 对讲收尾要发 TALK 0, 多等一会
        final double dur = o.optDouble("dur", 0);
        setMode(M_IDLE);
        ui.postDelayed(new Runnable() { public void run() { boardPlay(name, dur); } },
                wasLive ? 600 : 250);
    }

    void boardPlay(final String name, final double dur) {
        ctrlExec(new Runnable() { public void run() {
            String r = ctrl("PLAY " + name);
            if (r == null) {
                ui.post(new Runnable() { public void run() { toast("板子没连上"); }});
                return;
            }
            if (!r.startsWith("OK")) {
                ui.post(new Runnable() { public void run() {
                    toast("播放失败: " + r + (r.startsWith("BUSY") ? " (对讲中, 先停喊话)" : ""));
                }});
                return;
            }
            addLog("播放: " + name);
            ui.post(new Runnable() { public void run() {
                setMode(M_PLAY);
                if (playFinish != null) ui.removeCallbacks(playFinish);
                final long ms = (dur > 0 ? (long) (dur * 1000) : 30000) + 900;
                playFinish = new Runnable() { public void run() {
                    if (mode == M_PLAY && name.equals(playingName)) setMode(M_IDLE);
                }};
                ui.postDelayed(playFinish, ms);
            }});
        }});
        JSONObject o = findPreset(name);       // 电平镜像: 本地同步读同内容文件驱动, 不占链路
        if (o != null && !o.optString("file").isEmpty()) levelMirror(o.optString("file"));
    }

    void levelMirror(String file) {
        final String f = file;
        new Thread() { public void run() {
            FileInputStream in = null;
            try {
                in = new FileInputStream(new File(getFilesDir(), f));
                byte[] buf = new byte[FRAME * 2];
                long sent = 0, t0 = System.nanoTime();
                int n;
                while (mode == M_PLAY && (n = in.read(buf)) > 0) {
                    sent += n;
                    long want = sent * 1000000000L / 2 / RATE;
                    long cur = System.nanoTime() - t0;
                    if (cur < want) Thread.sleep(Math.min(60, (want - cur) / 1000000L));
                    long acc = 0;
                    int ns = n / 2;
                    for (int i = 0; i < ns; i++) {
                        short sv = (short) ((buf[i * 2] & 0xFF) | (buf[i * 2 + 1] << 8));
                        acc += (long) sv * sv;
                    }
                    postLevel(acc, Math.max(1, ns));
                }
            } catch (Exception ig) {
            } finally {
                if (in != null) { try { in.close(); } catch (Exception ig) {} }
            }
        }}.start();
    }

    void stopBoardPlay() {
        if (playFinish != null) { ui.removeCallbacks(playFinish); playFinish = null; }
        ctrlExec(new Runnable() { public void run() {
            ctrl("STOP");
            addLog("停止播放");
        }});
    }

    // ================= 试听(遥控器本地) =================

    void toggleAud(int idx) {
        JSONObject o = presets.optJSONObject(idx);
        if (o == null) return;
        if (mode == M_AUD && o.optString("name").equals(playingName)) {
            setMode(M_IDLE);
            return;
        }
        playingName = o.optString("name");
        final String file = o.optString("file");
        if (mode == M_IDLE) { audNow(file); return; }
        setMode(M_IDLE);
        ui.postDelayed(new Runnable() { public void run() { audNow(file); } }, 200);
    }

    void audNow(String file) {
        setMode(M_AUD);
        workThread = new Thread() { public void run() { audLoop(file); } };
        workThread.start();
    }

    void audLoop(String file) {
        AudioTrack track = null;
        FileInputStream in = null;
        try {
            File f = new File(getFilesDir(), file);
            if (!f.exists()) throw new IllegalStateException("音频文件丢失");
            in = new FileInputStream(f);
            int bufSize = Math.max(RATE * 2,
                    AudioTrack.getMinBufferSize(RATE, AudioFormat.CHANNEL_OUT_MONO,
                            AudioFormat.ENCODING_PCM_16BIT));
            track = new AudioTrack(AudioManager.STREAM_MUSIC, RATE,
                    AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT,
                    bufSize, AudioTrack.MODE_STREAM);
            track.play();
            byte[] buf = new byte[FRAME * 2];
            int n;
            while (mode == M_AUD && (n = in.read(buf)) > 0) {
                track.setVolume(curVol / 100f);   // 试听音量也跟随 App 音量
                track.write(buf, 0, n);
                long acc = 0;
                int ns = n / 2;
                for (int i = 0; i < ns; i++) {
                    short sv = (short) ((buf[i * 2] & 0xFF) | (buf[i * 2 + 1] << 8));
                    acc += (long) sv * sv;
                }
                postLevel(acc, Math.max(1, ns));
            }
        } catch (Exception e) {
            if (mode == M_AUD) toast("试听出错: " + e.getMessage());
        } finally {
            if (track != null) {
                try { track.stop(); } catch (Exception ig) {}
                try { track.release(); } catch (Exception ig) {}
            }
            if (in != null) { try { in.close(); } catch (Exception ig) {} }
            if (mode == M_AUD) setMode(M_IDLE);
        }
    }

    // ================= 文件导入(ImportActivity 回调) =================

    static void handleImport(Context ctx, Uri uri) {
        if (inst == null) return;
        inst.importFrom(uri);
    }

    void importFrom(final Uri uri) {
        String name = "导入音频";
        Cursor c = getContentResolver().query(uri, null, null, null, null);
        if (c != null) {
            try {
                if (c.moveToFirst()) {
                    int ci = c.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                    if (ci >= 0 && c.getString(ci) != null)
                        name = c.getString(ci).replaceAll("\\.[^.]+$", "");
                }
            } finally { c.close(); }
        }
        final String pname = name;
        toast("解码中…");
        new Thread() { public void run() { importUri(uri, pname); }}.start();
    }

    void importUri(Uri uri, String name) {
        try {
            InputStream is = getContentResolver().openInputStream(uri);
            File src = new File(getFilesDir(), "import_src_" + System.currentTimeMillis());
            FileOutputStream so = new FileOutputStream(src);
            byte[] buf = new byte[16384];
            int n;
            while ((n = is.read(buf)) > 0) so.write(buf, 0, n);
            is.close();
            so.close();
            importFile(src, name, true);
        } catch (Exception e) {
            final String msg = e.getMessage();
            ui.post(new Runnable() { public void run() { toast("读取失败: " + msg); }});
        }
    }

    // ================= 文字转语音(在线合成) =================

    void ttsDialog() {
        final EditText et = new EditText(this);
        et.setHint("输入要播报的文字");
        AlertDialog d = new AlertDialog.Builder(this)
                .setTitle("文字转语音 (合成后存入录音库并播放)")
                .setView(et)
                .setPositiveButton("合成并播放", new android.content.DialogInterface.OnClickListener() {
                    public void onClick(android.content.DialogInterface dg, int w) {
                        String text = et.getText().toString().trim();
                        if (text.length() == 0) { toast("先输入文字"); return; }
                        ttsSpeak(text);
                    }
                })
                .setNegativeButton("取消", null)
                .create();
        focusable(d);
        d.show();
    }

    /** 在线 TTS(百度翻译接口, 免 key): 文字 -> mp3 -> 解码入录音库 -> 播放到板子
     *  本地 skytts 引擎的 synthesizeToFile 有 bug(崩在 makeWavHeader), 故走在线 */
    void ttsSpeak(final String text) {
        toast("在线合成中…");
        new Thread() { public void run() {
            File out = null;
            try {
                String url = "https://fanyi.baidu.com/gettts?lan=zh&spd=3&source=web&text="
                        + java.net.URLEncoder.encode(text, "UTF-8");
                HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(6000);
                c.setReadTimeout(12000);
                c.setRequestProperty("User-Agent", "Mozilla/5.0");
                InputStream in = c.getInputStream();
                File dir = getExternalFilesDir("tts");
                if (dir == null) dir = getFilesDir();
                if (!dir.exists()) dir.mkdirs();
                out = new File(dir, "tts_" + System.currentTimeMillis() + ".mp3");
                FileOutputStream fo = new FileOutputStream(out);
                byte[] buf = new byte[8192];
                int n;
                long total = 0;
                while ((n = in.read(buf)) > 0) { fo.write(buf, 0, n); total += n; }
                fo.close();
                in.close();
                android.util.Log.i("PaTts", "download=" + total + " -> " + out);
                if (total < 1000) throw new IllegalStateException("返回数据异常(" + total + ")");
                final String name = text.length() > 10 ? text.substring(0, 10) : text;
                final File src = out;
                if (importFile(src, name, true)) {
                    ui.post(new Runnable() { public void run() {
                        toast("已合成: " + name);
                        for (int i = presets.length() - 1; i >= 0; i--) {
                            JSONObject o = presets.optJSONObject(i);
                            if (o != null && name.equals(o.optString("name"))) {
                                playPreset(i);
                                break;
                            }
                        }
                    }});
                }
            } catch (Exception e) {
                final String msg = e.getMessage();
                if (out != null) out.delete();
                ui.post(new Runnable() { public void run() {
                    toast("合成失败: " + msg + " (检查遥控器网络)");
                }});
            }
        }}.start();
    }

    /** 扫描 /sdcard/Download 列出音频, 点一下导入(G12 的文件选择器卡死, 走这条路) */
    void scanDownload() {
        if (mode != M_IDLE) { toast("先停止喊话/播放再导入"); return; }
        if (Build.VERSION.SDK_INT >= 23
                && checkSelfPermission(android.Manifest.permission.READ_EXTERNAL_STORAGE)
                != PackageManager.PERMISSION_GRANTED) {
            toast("缺存储权限, 到喊话台主界面点「授予存储权限」");
            return;
        }
        File dir = new File("/sdcard/Download");
        android.util.Log.i("PaScan", "dir=" + dir + " exists=" + dir.exists()
                + " canRead=" + dir.canRead() + " readable=" + dir.canRead());
        File[] all = dir.listFiles();
        android.util.Log.i("PaScan", "listFiles=" + (all == null ? "null" : "" + all.length));
        if (all == null) {
            dir = new File("/storage/emulated/0/Download");
            all = dir.listFiles();
            android.util.Log.i("PaScan", "alt listFiles=" + (all == null ? "null" : "" + all.length));
        }
        final ArrayList<File> audio = new ArrayList<File>();
        if (all != null) {
            StringBuilder sb = new StringBuilder();
            for (File f : all) {
                if (sb.length() < 200) sb.append(f.getName()).append("(f=")
                        .append(f.isFile() ? 1 : 0).append(") ");
                String n = f.getName().toLowerCase();
                if (n.endsWith(".mp3") || n.endsWith(".m4a")
                        || n.endsWith(".wav") || n.endsWith(".aac") || n.endsWith(".ogg"))
                    audio.add(f);
            }
            android.util.Log.i("PaScan", "names=" + sb);
        }
        if (audio.isEmpty()) {
            toast("Download 没有音频 (共 " + (all == null ? 0 : all.length) + " 个文件)");
            return;
        }
        android.util.Log.i("PaScan", "audio=" + audio.size());
        // 直接导入(同名已存在则跳过), 不走对话框(G12 上 overlay 对话框不稳)
        toast("发现 " + audio.size() + " 个音频, 开始导入…");
        new Thread() { public void run() {
            int added = 0, skipped = 0;
            for (File f : audio) {
                String nm = f.getName().replaceAll("\\.[^.]+$", "");
                if (nameExists(nm)) { skipped++; continue; }
                if (importFile(f, nm, false)) added++;
            }
            final int a = added, sk = skipped;
            ui.post(new Runnable() { public void run() {
                toast("导入完成: 新增 " + a + " 个" + (sk > 0 ? ", 跳过重复 " + sk + " 个" : ""));
            }});
        }}.start();
    }

    boolean nameExists(String nm) {
        for (int i = 0; i < presets.length(); i++) {
            JSONObject o = presets.optJSONObject(i);
            if (o != null && nm.equals(o.optString("name"))) return true;
        }
        return false;
    }

    boolean importFile(File src, String name, boolean deleteSrc) {
        MediaExtractor ex = new MediaExtractor();
        MediaCodec codec = null;
        File tmp = new File(getFilesDir(), "imp_tmp.pcm");
        FileOutputStream fo = null;
        try {
            ex.setDataSource(src.getAbsolutePath());
            int trackIdx = -1;
            MediaFormat fmt = null;
            for (int i = 0; i < ex.getTrackCount(); i++) {
                MediaFormat f = ex.getTrackFormat(i);
                String mime = f.getString(MediaFormat.KEY_MIME);
                if (mime != null && mime.startsWith("audio/")) {
                    trackIdx = i; fmt = f; break;
                }
            }
            if (trackIdx < 0) throw new IllegalArgumentException("找不到音轨");
            ex.selectTrack(trackIdx);
            String mime = fmt.getString(MediaFormat.KEY_MIME);
            codec = MediaCodec.createDecoderByType(mime);
            codec.configure(fmt, null, null, 0);
            codec.start();
            fo = new FileOutputStream(tmp);
            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            int outRate = RATE, outCh = 1;
            Resampler rs = null;
            boolean eos = false;
            long totalFrames = 0;
            while (!eos) {
                int ib = codec.dequeueInputBuffer(10000);
                if (ib >= 0) {
                    java.nio.ByteBuffer bb = codec.getInputBuffer(ib);
                    int sz = ex.readSampleData(bb, 0);
                    if (sz >= 0) {
                        bb.position(sz);
                        codec.queueInputBuffer(ib, 0, sz, ex.getSampleTime(), 0);
                        ex.advance();
                    } else {
                        codec.queueInputBuffer(ib, 0, 0, 0,
                                MediaCodec.BUFFER_FLAG_END_OF_STREAM);
                    }
                }
                int ob = codec.dequeueOutputBuffer(info, 10000);
                if (ob == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    MediaFormat of = codec.getOutputFormat();
                    outRate = of.containsKey(MediaFormat.KEY_SAMPLE_RATE)
                            ? of.getInteger(MediaFormat.KEY_SAMPLE_RATE) : RATE;
                    outCh = of.containsKey(MediaFormat.KEY_CHANNEL_COUNT)
                            ? of.getInteger(MediaFormat.KEY_CHANNEL_COUNT) : 1;
                    rs = new Resampler(outRate, outCh);
                } else if (ob >= 0) {
                    java.nio.ByteBuffer ob2 = codec.getOutputBuffer(ob);
                    if (ob2 != null && info.size > 0) {
                        if (rs == null) rs = new Resampler(outRate, outCh);
                        ob2.position(info.offset);
                        ob2.limit(info.offset + info.size);
                        int ns = info.size / 2 / Math.max(1, outCh);
                        short[] in = new short[ns * Math.max(1, outCh)];
                        ob2.order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().get(in);
                        short[] out16 = rs.push(in);
                        byte[] oby = new byte[out16.length * 2];
                        for (int i = 0; i < out16.length; i++) {
                            oby[i * 2] = (byte) (out16[i] & 0xFF);
                            oby[i * 2 + 1] = (byte) ((out16[i] >> 8) & 0xFF);
                        }
                        fo.write(oby);
                        totalFrames += out16.length;
                    }
                    codec.releaseOutputBuffer(ob, false);
                    if ((info.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) eos = true;
                }
            }
            fo.close();
            fo = null;
            if (deleteSrc) src.delete();
            if (totalFrames < 1600) throw new IllegalArgumentException("解码后太短");
            final String file = "preset_" + System.currentTimeMillis() + ".pcm";
            new File(getFilesDir(), file).delete();
            tmp.renameTo(new File(getFilesDir(), file));
            addPresetEntry(name, file, totalFrames / (double) RATE);
            enqueueUpload(name, false);
            ui.post(new Runnable() { public void run() {
                toast("已添加: " + name);
            }});
            return true;
        } catch (Exception e) {
            tmp.delete();
            final String msg = e.getMessage();
            ui.post(new Runnable() { public void run() {
                toast("导入失败: " + msg + " (建议用 mp3/m4a/wav)");
            }});
            return false;
        } finally {
            if (fo != null) { try { fo.close(); } catch (Exception ig) {} }
            if (codec != null) {
                try { codec.stop(); } catch (Exception ig) {}
                try { codec.release(); } catch (Exception ig) {}
            }
            try { ex.release(); } catch (Exception ig) {}
        }
    }

    // ================= 板端文件库(同步/上传/丢包日志) =================

    void addLog(String s) {
        String line = new SimpleDateFormat("HH:mm:ss", Locale.CHINA)
                .format(new Date()) + " " + s;
        synchronized (logLock) {
            logs.addLast(line);
            while (logs.size() > 60) logs.removeFirst();
        }
        android.util.Log.i("PaLog", s);
    }

    /** 只留板端合法字符(中文/字母/数字/_-), 空格全角等一并剔除, 协议按空格分词 */
    String sanitizeName(String s) {
        StringBuilder b = new StringBuilder();
        for (char c : s.trim().toCharArray()) {
            if ((c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
                    || c == '_' || c == '-' || (c >= 0x4e00 && c <= 0x9fff)) b.append(c);
        }
        String r = b.toString();
        if (r.isEmpty()) r = "audio_" + (System.currentTimeMillis() % 10000);
        return r.substring(0, Math.min(40, r.length()));
    }

    JSONObject findPreset(String name) {
        for (int i = 0; i < presets.length(); i++) {
            JSONObject o = presets.optJSONObject(i);
            if (o != null && name.equals(o.optString("name"))) return o;
        }
        return null;
    }

    double findDur(String name) {
        JSONObject o = findPreset(name);
        return o == null ? 0 : o.optDouble("dur", 0);
    }

    void markBst(String name, int v) {
        JSONObject o = findPreset(name);
        if (o != null) {
            try { o.put("bst", v); } catch (Exception ig) {}
        }
    }

    void updateRateBtn() {
        if (rateBtn != null) {
            rateBtn.setText("话质:" + (talkRate >= 16000 ? "高" : "省"));
            rateBtn.setTextColor(talkRate >= 16000 ? 0xFF64748B : 0xFFFBBF24);
        }
    }

    void toggleTalkRate() {
        talkRate = talkRate >= 16000 ? 8000 : 16000;
        prefs.edit().putInt("talkRate", talkRate).apply();
        updateRateBtn();
        ctrlExec(new Runnable() { public void run() { ctrl("RATE " + talkRate); }});
        toast("对讲话质: " + (talkRate >= 16000 ? "高清 16k" : "省 8k (丢包不随码率降, 省一半带宽)"));
        addLog("话质 -> " + talkRate + "Hz");
    }

    /** LIST: 板端回 name|size|crc32 多包(LST i/n) */
    java.util.HashMap<String, long[]> listBoard() {
        java.util.HashMap<String, long[]> m = new java.util.HashMap<String, long[]>();
        DatagramSocket s = null;
        try {
            s = new DatagramSocket();
            s.setSoTimeout(1200);
            s.connect(InetAddress.getByName(DST_IP), CTRL_PORT);
            byte[] ob = "LIST".getBytes();
            s.send(new DatagramPacket(ob, ob.length));
            java.util.HashMap<Integer, String> parts = new java.util.HashMap<Integer, String>();
            int total = -1;
            long t0 = SystemClock.elapsedRealtime();
            while (SystemClock.elapsedRealtime() - t0 < 2500
                    && (total < 0 || parts.size() < total)) {
                byte[] rb = new byte[1600];
                DatagramPacket rp = new DatagramPacket(rb, rb.length);
                try {
                    s.receive(rp);
                } catch (Exception e) {
                    break;
                }
                String line = new String(rb, 0, rp.getLength(), "UTF-8");
                if (!line.startsWith("LST ")) continue;
                int nl = line.indexOf('\n');
                if (nl < 0) continue;
                String[] kn = line.substring(4, nl).split("/");
                try {
                    total = Integer.parseInt(kn[1]);
                    parts.put(Integer.parseInt(kn[0]), line.substring(nl + 1));
                } catch (Exception ig) {}
            }
            for (int i = 1; i <= total; i++) {
                String p = parts.get(i);
                if (p == null) continue;
                for (String ln : p.split("\n")) {
                    String[] seg = ln.trim().split("\\|");
                    if (seg.length == 3) {
                        try {
                            m.put(seg[0], new long[]{Long.parseLong(seg[1]),
                                    Long.parseLong(seg[2], 16)});
                        } catch (Exception ig) {}
                    }
                }
            }
        } catch (Exception ig) {
        } finally {
            if (s != null) s.close();
        }
        return m;
    }

    long fileCrc(File f) {
        java.util.zip.CRC32 c = new java.util.zip.CRC32();
        try {
            FileInputStream in = new FileInputStream(f);
            byte[] b = new byte[65536];
            int n;
            while ((n = in.read(b)) > 0) c.update(b, 0, n);
            in.close();
        } catch (Exception e) {
            return -1;
        }
        return c.getValue();
    }

    /** ctrlPool 调度: 服务销毁后 ui.post/上传回调的迟到调用不再炸进程 */
    void ctrlExec(Runnable r) {
        try { ctrlPool.execute(r); }
        catch (java.util.concurrent.RejectedExecutionException ig) {}
    }

    /** upPool 调度: 同 ctrlExec; 已关池时把名字清出 uploading 防"永久同步中" */
    void upExec(final String name, Runnable r) {
        try { upPool.execute(r); }
        catch (java.util.concurrent.RejectedExecutionException ig) {
            if (name != null) uploading.remove(name);
        }
    }

    /** 对账: 本地条目标 ✓/⚠, 板上独有的补进列表(可直接播) */
    void syncLibrary() {
        if (upPool.isShutdown()) return;   // 服务已销毁, 探活线程迟到的同步别再炸进程
        upExec(null, new Runnable() { public void run() {
            final java.util.HashMap<String, long[]> board = listBoard();
            if (board.isEmpty() && ctrl("HELLO") == null) {
                addLog("同步失败: 板子没连上");
                return;
            }
            ui.post(new Runnable() { public void run() {
                int changed = 0;
                for (int i = 0; i < presets.length(); i++) {
                    JSONObject o = presets.optJSONObject(i);
                    if (o == null) continue;
                    long[] bc = board.get(o.optString("name"));
                    int bst;
                    if (bc == null) bst = 0;
                    else {
                        String lf = o.optString("file");
                        bst = (lf.isEmpty()
                                || fileCrc(new File(getFilesDir(), lf)) == bc[1]) ? 1 : 2;
                    }
                    if (o.optInt("bst", -1) != bst) {
                        try { o.put("bst", bst); } catch (Exception ig) {}
                        changed++;
                    }
                }
                for (java.util.Map.Entry<String, long[]> en : board.entrySet()) {
                    if (!nameExists(en.getKey())) {
                        try {
                            JSONObject o = new JSONObject();
                            o.put("name", en.getKey());
                            o.put("file", "");
                            o.put("dur", Math.round(en.getValue()[0] / 32000.0 * 10) / 10.0);
                            o.put("bst", 1);
                            presets.put(o);
                            changed++;
                        } catch (Exception ig) {}
                    }
                }
                if (changed > 0) {
                    savePresets();
                    buildChips();
                    if (libDialog != null) showLibrary();
                }
                if (libDialog != null)                  // 库标题常显板上容量
                    libDialog.setTitle("录音库 板上 " + board.size() + "/" + BOARD_MAX_FILES + " 条");
                addLog("同步完成: 板上 " + board.size() + " 条音频");
            }});
        }});
    }

    /** 完整性优先的上传: 板端 crc 校验通过(DONE)才算数; thenPlay=传完立即播 */
    void enqueueUpload(final String name, final boolean thenPlay) {
        if (uploading.contains(name)) {
            if (thenPlay) autoPlay.add(name);
            return;
        }
        if (upPool.isShutdown()) return;   // 录音收尾/导入/TTS 迟到路径: 服务已销毁别再炸进程
        uploading.add(name);
        if (thenPlay) autoPlay.add(name);
        upExec(name, new Runnable() { public void run() {
            for (int i = 0; i < 30 && mode == M_LIVE; i++) {   // 对讲带宽优先, 喊完再传
                try { Thread.sleep(2000); } catch (InterruptedException e) {
                    uploading.remove(name);
                    return;
                }
            }
            JSONObject o = findPreset(name);
            boolean okk = false;
            String msg;
            if (o == null) msg = "条目不存在";
            else if (o.optString("file").isEmpty()) msg = "无本地副本";
            else {
                File f = new File(getFilesDir(), o.optString("file"));
                if (!f.exists()) msg = "本地文件丢失";
                else {
                    try {
                        byte[] data = new byte[(int) f.length()];
                        FileInputStream in = new FileInputStream(f);
                        int rd = 0;
                        while (rd < data.length) {
                            int k = in.read(data, rd, data.length - rd);
                            if (k < 0) break;
                            rd += k;
                        }
                        in.close();
                        long t0 = SystemClock.elapsedRealtime();
                        okk = uploadToBoard(name, data);
                        if (!okk && !lastUpErr.isEmpty()) msg = lastUpErr;
                        else msg = String.format(Locale.CHINA, "%.0fKB/%.1fs",
                                data.length / 1024.0,
                                (SystemClock.elapsedRealtime() - t0) / 1000.0);
                    } catch (Exception e) {
                        msg = "读取失败";
                    }
                }
            }
            uploading.remove(name);
            final boolean fok = okk;
            final String fmsg = msg;
            addLog("上传 " + name + " " + (fok ? "完成 " : "失败: ") + fmsg);
            ui.post(new Runnable() { public void run() {
                renderMode();                          // 清掉 hint 里的"同步中 xx%"
                if (libDialog != null) showLibrary();
                toast("同步 " + name + (fok ? " 完成" : " 失败: " + fmsg));
                if (fok) {
                    markBst(name, 1);
                    savePresets();
                    if (autoPlay.remove(name)) boardPlay(name, findDur(name));
                } else {
                    markBst(name, 2);
                    autoPlay.remove(name);   // 失败也清: 别让陈旧自动播等下次上传成功突然响起
                    savePresets();
                }
            }});
        }});
    }

    /** UPB 起始 / UPC 分块(累计确认, 滑窗12, 丢块重传) / UPE 板端整文件 crc 校验后落盘 */
    boolean uploadToBoard(String name, byte[] data) {
        DatagramSocket s = null;
        try {
            s = new DatagramSocket();
            InetAddress dst = InetAddress.getByName(DST_IP);
            final String sid = Integer.toHexString(
                    (int) (System.currentTimeMillis() & 0xFFFFL));
            java.util.zip.CRC32 cz = new java.util.zip.CRC32();
            cz.update(data);
            boolean began = false;
            String r0 = "";
            for (int t = 0; t < 3 && !began; t++) {
                byte[] ob = ("UPB " + sid + " " + name + " " + data.length
                        + " " + String.format("%08x", cz.getValue())).getBytes("UTF-8");
                s.send(new DatagramPacket(ob, ob.length, dst, UP_PORT));
                r0 = waitAck(s, 900);
                began = r0.startsWith("OK");
            }
            if (!began) {
                lastUpErr = r0.isEmpty() ? "板子无应答" : r0;   // ERR MAXFILES/NOSPACE 等原样带给用户
                return false;
            }
            lastUpErr = "";
            int base = 0, off = 0, idle = 0, lastPct = -100;
            while (base < data.length) {
                while (off < data.length && off - base < CHUNK * 12) {
                    byte[] pk = upChunk(sid, off, data);
                    s.send(new DatagramPacket(pk, pk.length, dst, UP_PORT));
                    off += CHUNK;
                    try { Thread.sleep(2); } catch (InterruptedException ig) { return false; }
                }
                String a = waitAck(s, 900);
                int nb = -1;
                if (a.startsWith("ACK")) {
                    try { nb = Integer.parseInt(a.split(" ")[2]); } catch (Exception ig) {}
                }
                while (true) {                         // 排干积压 ACK, 取最大已确认前缀
                    String a2 = waitAck(s, 25);
                    if (!a2.startsWith("ACK")) break;
                    try { nb = Math.max(nb, Integer.parseInt(a2.split(" ")[2])); }
                    catch (Exception ig) {}
                }
                if (nb >= 0) { base = nb; idle = 0; }
                else { idle++; off = base; if (idle > 50) return false; }
                int pct = base * 100 / data.length;
                if (pct >= lastPct + 20) {
                    lastPct = pct;
                    final int fp = pct;
                    ui.post(new Runnable() { public void run() {
                        if (expanded) hint.setText("↑ 同步到板子… " + fp + "%");
                    }});
                }
            }
            for (int t = 0; t < 5; t++) {
                byte[] ob = ("UPE " + sid).getBytes();
                s.send(new DatagramPacket(ob, ob.length, dst, UP_PORT));
                String r = waitAck(s, 1500);
                if (r.startsWith("DONE")) return true;
                if (r.startsWith("FAIL")) return false;
            }
            return false;
        } catch (Exception e) {
            return false;
        } finally {
            if (s != null) s.close();
        }
    }

    byte[] upChunk(String sid, int off, byte[] data) {
        byte[] h = ("UPC " + sid + " " + off + " ").getBytes();
        int n = Math.min(CHUNK, data.length - off);
        byte[] o = new byte[h.length + n];
        System.arraycopy(h, 0, o, 0, h.length);
        System.arraycopy(data, off, o, h.length, n);
        return o;
    }

    String waitAck(DatagramSocket s, int ms) {
        try {
            s.setSoTimeout(ms);
            byte[] rb = new byte[512];
            DatagramPacket rp = new DatagramPacket(rb, rb.length);
            s.receive(rp);
            return new String(rb, 0, rp.getLength(), "UTF-8");
        } catch (Exception e) {
            return "";
        }
    }

    void showLogs() {
        StringBuilder sb = new StringBuilder();
        synchronized (logLock) {
            for (String l : logs) sb.append(l).append('\n');
        }
        TextView tv = new TextView(this);
        tv.setText(sb.length() == 0 ? "(还没有日志)" : sb.toString());
        tv.setTextSize(11);
        tv.setTextColor(0xFFCBD5E1);
        tv.setTypeface(Typeface.MONOSPACE);
        tv.setPadding(dp(12), dp(10), dp(12), dp(10));
        ScrollView sc2 = new ScrollView(this);
        sc2.addView(tv);
        AlertDialog d = new AlertDialog.Builder(this)
                .setTitle("传输日志 (对讲丢包/上传, 最近60条)")
                .setView(sc2)
                .setPositiveButton("清空", new android.content.DialogInterface.OnClickListener() {
                    public void onClick(android.content.DialogInterface dg, int w) {
                        synchronized (logLock) { logs.clear(); }
                    }
                })
                .setNegativeButton("关闭", null)
                .create();
        d.getWindow().setType(overlayType());
        d.show();
    }

    // ================= 探活 + 音量 =================

    String ctrl(String cmd) {
        DatagramSocket s = null;
        try {
            s = new DatagramSocket();
            s.setSoTimeout(1500);
            s.connect(InetAddress.getByName(DST_IP), CTRL_PORT);
            byte[] ob = cmd.getBytes();
            s.send(new DatagramPacket(ob, ob.length));
            byte[] rb = new byte[256];
            DatagramPacket rp = new DatagramPacket(rb, rb.length);
            s.receive(rp);
            return new String(rb, 0, rp.getLength());
        } catch (Exception e) {
            return null;
        } finally {
            if (s != null) s.close();
        }
    }

    void setVol(int v) {
        final int nv = Math.max(0, Math.min(VOL_MAX, v));
        curVol = nv;                       // 本地即时生效并显示; 板端回显一律不覆盖(防回溯)
        ui.post(new Runnable() { public void run() {
            volText.setText(nv + "%");
        }});
        if (v > VOL_MAX) toast("已是最大音量 " + VOL_MAX + "%");
        queueVol(nv);
    }

    /** 连点合并: 队列里只认最新值, 前面作废; 单发不排队 */
    void queueVol(final int v) {
        pendingVol = v;
        ctrlExec(new Runnable() { public void run() {
            if (pendingVol != v) return;
            ctrl("VOL " + v);
        }});
    }

    void startProbe() {
        probeThread = new Thread() { public void run() {
            boolean prevOk = false;
            while (true) {
                final boolean ok = ctrl("HELLO") != null;
                if (ok != prevOk) {
                    prevOk = ok;
                    addLog("板子" + (ok ? "上线" : "失联"));
                }
                if (ok && !firstSync) {
                    firstSync = true;
                    syncLibrary();                 // 首次上线: 拉板端文件库, 补条目+核 crc
                }
                if (ok && !volSynced) {            // 只在首次上线同步一次板端音量, 之后不再回读
                    volSynced = true;
                    String v = ctrl("VOL?");
                    if (v != null && v.startsWith("VOL ")) {
                        try {
                            final int cur = Math.max(0, Math.min(VOL_MAX,
                                    Integer.parseInt(v.substring(4).trim())));
                            curVol = cur;
                            ui.post(new Runnable() { public void run() {
                                volText.setText(cur + "%");
                            }});
                        } catch (Exception ig) {}
                    }
                }
                ui.post(new Runnable() { public void run() {
                    statusText.setText(ok ? "●" : "○");
                    statusText.setTextColor(ok ? 0xFF34D399 : 0xFF64748B);
                }});
                try { Thread.sleep(3000); } catch (InterruptedException e) { return; }
            }
        }};
        probeThread.setDaemon(true);
        probeThread.start();
    }

    // ================= 更新(自建下载) =================

    boolean canInstall() {
        if (Build.VERSION.SDK_INT >= 26) {
            try {
                return getPackageManager().canRequestPackageInstalls();
            } catch (Exception e) {
                return false;   // 权限未声明等异常场景走引导, 不崩
            }
        }
        return true;
    }

    void onUpdateClicked() {
        if (latestVer > APP_VERSION) {
            if (!canInstall()) {
                toast("先去开「允许安装未知应用」, 开完回来再点一次");
                try {
                    startActivity(new Intent(
                            android.provider.Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                            Uri.parse("package:" + getPackageName()))
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
                } catch (Exception e) {
                    toast("请在系统设置里允许本应用安装未知应用");
                }
                return;
            }
            startDownload();
        } else {
            checkUpdate(true);
        }
    }

    void checkUpdate(final boolean manual) {
        ctrlExec(new Runnable() { public void run() {
            int latest = -1;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(
                        "http://" + DST_IP + ":" + PAGE_PORT + "/version.json").openConnection();
                c.setConnectTimeout(2500);
                c.setReadTimeout(2500);
                InputStream in = c.getInputStream();
                ByteArrayOutputStream bo = new ByteArrayOutputStream();
                byte[] buf = new byte[512];
                int n;
                while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
                in.close();
                Matcher m = VER_PAT.matcher(bo.toString("UTF-8"));
                if (m.find()) latest = Integer.parseInt(m.group(1));
            } catch (Exception ig) {}
            latestVer = latest;
            final int ver = latest;
            ui.post(new Runnable() { public void run() {
                refreshUpdBtn();
                if (manual) {
                    toast(ver == APP_VERSION ? "已是最新版本"
                            : ver > APP_VERSION ? "发现新版 v" + ver
                            : "查不到新版本(板子没连上?)");
                }
            }});
        }});
    }

    void startDownload() {
        if (dlBusy) { toast("正在下载中…"); return; }
        dlBusy = true;
        if (updBtn != null) {
            updBtn.setTextColor(0xFF60A5FA);
            updBtn.setText("下载中…");
        }
        new Thread() { public void run() {
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(
                        "http://" + DST_IP + ":" + PAGE_PORT + "/PaSend.apk").openConnection();
                c.setConnectTimeout(3000);
                c.setReadTimeout(8000);
                int total = c.getContentLength();
                InputStream in = c.getInputStream();
                File dir = getExternalFilesDir("update");
                if (dir == null) dir = getFilesDir();
                File out = new File(dir, "update.apk");
                FileOutputStream fo = new FileOutputStream(out);
                byte[] buf = new byte[8192];
                int n, done = 0;
                while ((n = in.read(buf)) > 0) {
                    fo.write(buf, 0, n);
                    done += n;
                    if (total > 0) {
                        final int pct = done * 100 / total;
                        ui.post(new Runnable() { public void run() {
                            if (updBtn != null) updBtn.setText("下载 " + pct + "%");
                        }});
                    }
                }
                fo.close();
                in.close();
                ui.post(new Runnable() { public void run() {
                    if (updBtn != null) updBtn.setText("下载完成, 弹出安装…");
                }});
                installUpdate();
            } catch (Exception e) {
                ui.post(new Runnable() { public void run() {
                    if (updBtn != null) {
                        updBtn.setText("更新失败, 点我重试");
                        updBtn.setTextColor(0xFFF87171);
                    }
                    toast("下载失败: " + e.getMessage());
                }});
            } finally {
                dlBusy = false;
            }
        }}.start();
    }

    void installUpdate() {
        try {
            Intent i = new Intent(Intent.ACTION_VIEW);
            i.setDataAndType(PaFiles.apkUri(), "application/vnd.android.package-archive");
            i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(i);
        } catch (Exception e) {
            toast("弹安装失败, 用浏览器开 http://" + DST_IP + ":" + PAGE_PORT + " 手动装");
        }
    }
}

/** 线性插值重采样到 16k 单声道; 实例持有跨包状态保证输出连续 */
class Resampler {
    final int rate, ch;
    final double grid;
    long outK = 0;
    long inBase = 0;
    float lastSample = 0;

    Resampler(int rate, int ch) {
        this.rate = Math.max(1, rate);
        this.ch = Math.max(1, ch);
        this.grid = (double) this.rate / 16000;
    }

    short[] push(short[] in) {
        int n = in.length / ch;
        if (n == 0) return new short[0];
        float[] mono = new float[n];
        for (int i = 0; i < n; i++) {
            float s = 0;
            for (int j = 0; j < ch; j++) s += in[i * ch + j];
            mono[i] = s / ch;
        }
        if (rate == 16000) {
            lastSample = mono[n - 1];
            inBase += n;
            outK += n;
            short[] out = new short[n];
            for (int i = 0; i < n; i++) out[i] = clamp(mono[i]);
            return out;
        }
        ArrayList<Short> outs = new ArrayList<Short>();
        while (true) {
            double t = outK * grid;
            double rel = t - inBase;
            int i0 = (int) Math.floor(rel);
            if (i0 + 1 >= n) break;
            float a = (i0 < 0) ? lastSample : mono[i0];
            float b = mono[i0 + 1];
            double frac = rel - Math.floor(rel);
            outs.add(clamp(a + (float) ((b - a) * frac)));
            outK++;
        }
        lastSample = mono[n - 1];
        inBase += n;
        short[] out = new short[outs.size()];
        for (int i = 0; i < outs.size(); i++) out[i] = outs.get(i);
        return out;
    }

    static short clamp(float v) {
        return v > 32767 ? 32767 : v < -32768 ? -32768 : (short) v;
    }
}
