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

    static final int APP_VERSION = 55;
    static final String DEFAULT_IP = "192.168.144.10";   // 板子默认地址(全工程唯一字面量); 实际地址由主界面 ⚙ 设置(prefs 键 board_ip)
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

    // ================= 板子地址(运行时读) =================

    /** 运行时读板子地址: prefs 键 board_ip(主界面 ⚙ 设置), 空串回退默认;
     *  所有 UDP/HTTP 目标(15000/15001/15003/15010 与控制台页面)每次发包前都从这里取 */
    static String boardIp(Context ctx) {
        try {
            String ip = ctx.getSharedPreferences("pa_prefs", Context.MODE_PRIVATE)
                    .getString("board_ip", DEFAULT_IP).trim();
            return ip.isEmpty() ? DEFAULT_IP : ip;
        } catch (Exception e) {
            return DEFAULT_IP;
        }
    }

    /** PaService 内部用(this 即 Context) */
    String dstIp() { return boardIp(this); }

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
        if (i != null && "gas".equals(i.getStringExtra("panel"))) {
            ui.post(new Runnable() { public void run() { showGasPanel(); }});
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
        try { wm.removeView(gasPanel); } catch (Exception ig) {}
        super.onDestroy();
    }

    // ================= 悬浮窗基础 =================

    WindowManager.LayoutParams gasLp;
    LinearLayout gasPanel;
    TextView gasFoot;
    final TextView[] gasRowName = new TextView[6];
    final TextView[] gasRowVal = new TextView[6];
    final TextView[] gasRowSt = new TextView[6];
    Thread gasThr;
    volatile boolean gasRun = false;

    /** 气体检测悬浮窗(v49, 替换驱鸟台): 半透明卡面(70% 不透明) + 6 路数值。
     *  场景=边看相机画面边看气体值, 数据源 板:/gas 每 2s 轮询(仅悬浮窗开着时轮)。 */
    void buildGasPanel() {
        gasLp = baseLp("gaspanel", 24, 420);
        gasLp.width = dp(222);
        gasPanel = new LinearLayout(this);
        gasPanel.setOrientation(LinearLayout.VERTICAL);
        gasPanel.setBackground(round(0xB3252628, 16));   // 半透明: 卡色 70%(11-04 需求)
        gasPanel.setPadding(dp(12), dp(2), dp(12), dp(10));
        dragHandler(gasPanel, gasPanel, gasLp, "gaspanel", null);

        // 标题行: 气体检测 + 控制台 + ✕
        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.HORIZONTAL);
        top.setGravity(Gravity.CENTER_VERTICAL);
        top.setPadding(0, dp(8), 0, dp(4));
        TextView tt = new TextView(this);
        tt.setText("气体检测");
        tt.setTextColor(PaTheme.TXT);
        tt.setTextSize(14);
        tt.setTypeface(Typeface.DEFAULT_BOLD);
        top.addView(tt, new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        TextView con = mkGhost("控制台");
        con.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                closeGasPanel(false);
                Intent it = new Intent(PaService.this, PaSend.class);
                it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(it);
            }
        });
        top.addView(con);
        TextView exit = mkGhost("✕");
        exit.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { closeGasPanel(true); }
        });
        top.addView(exit);
        dragHandler(top, gasPanel, gasLp, "gaspanel", null);
        gasPanel.addView(top, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        // 2 列 x 3 行网格(11-04: 竖排 6 行占横屏 72% 高, 拖到底被钳位=「黏底」体感;
        // 改网格后面板高度砍半, 拖动空间翻倍)
        for (int row = 0; row < 3; row++) {
            LinearLayout r = new LinearLayout(this);
            r.setOrientation(LinearLayout.HORIZONTAL);
            for (int col = 0; col < 2; col++) {
                final int idx = row * 2 + col;
                // 两行式格子(11-05: 单行"名+点+值"在 222dp 两列里名字被数值挤没, CO2/HCN/CH4 全截断)
                LinearLayout cell = new LinearLayout(this);
                cell.setOrientation(LinearLayout.VERTICAL);
                cell.setPadding(dp(6), dp(4), dp(6), dp(3));
                LinearLayout nmRow = new LinearLayout(this);
                nmRow.setOrientation(LinearLayout.HORIZONTAL);
                nmRow.setGravity(Gravity.CENTER_VERTICAL);
                TextView nm = new TextView(this);
                nm.setText("— " + (idx + 1));
                nm.setTextColor(PaTheme.TXT2);
                nm.setTextSize(11f);
                nm.setSingleLine(true);
                nmRow.addView(nm, new LinearLayout.LayoutParams(0,
                        LinearLayout.LayoutParams.WRAP_CONTENT, 1));
                TextView st = new TextView(this);
                st.setText("");
                st.setTextSize(9);
                st.setPadding(dp(4), 0, 0, 0);
                nmRow.addView(st);
                cell.addView(nmRow, new LinearLayout.LayoutParams(
                        LinearLayout.LayoutParams.MATCH_PARENT,
                        LinearLayout.LayoutParams.WRAP_CONTENT));
                TextView vv = new TextView(this);
                vv.setText("—");
                vv.setTextColor(PaTheme.TXT);
                vv.setTextSize(16);
                vv.setTypeface(Typeface.MONOSPACE);
                vv.setSingleLine(true);
                cell.addView(vv, new LinearLayout.LayoutParams(
                        LinearLayout.LayoutParams.MATCH_PARENT,
                        LinearLayout.LayoutParams.WRAP_CONTENT));
                LinearLayout.LayoutParams clp = new LinearLayout.LayoutParams(
                        0, LinearLayout.LayoutParams.WRAP_CONTENT, 1);
                if (col > 0) clp.leftMargin = dp(10);
                r.addView(cell, clp);
                gasRowName[idx] = nm;
                gasRowVal[idx] = vv;
                gasRowSt[idx] = st;
            }
            gasPanel.addView(r, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        }
        gasFoot = new TextView(this);
        gasFoot.setText("连接板子…");
        gasFoot.setTextColor(PaTheme.TXT2);
        gasFoot.setTextSize(10);
        gasFoot.setPadding(0, dp(7), 0, 0);
        gasPanel.addView(gasFoot, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
    }

    /** 一键上传(v54): 所有未上板且有本地副本的条目入队, upPool 串行逐个传 */
    void uploadAll() {
        int n = 0;
        for (int i = 0; i < presets.length(); i++) {
            JSONObject o = presets.optJSONObject(i);
            if (o == null || o.optInt("bst", 0) == 1) continue;
            if (o.optString("file").isEmpty()) continue;
            String nm = o.optString("name");
            if (uploading.contains(nm)) continue;
            enqueueUpload(nm, false);
            n++;
        }
        toast(n == 0 ? "没有需要上传的(都已同步)" : "全部上传: " + n + " 条排队");
        addLog("全部上传 " + n + " 条");
    }

    void closeGasPanel(boolean toastIt) {
        gasRun = false;
        try { wm.removeView(gasPanel); } catch (Exception ig) {}
        if (panel.getParent() == null && bubble.getParent() == null) {
            mode = M_IDLE;
            stopForeground(true);
            stopSelf();
        }
        if (toastIt) toast("气体检测已关闭");
    }

    void showGasPanel() {
        if (gasPanel == null) buildGasPanel();
        if (gasPanel.getParent() == null) wm.addView(gasPanel, gasLp);
        if (!gasRun) startGasPoll();
    }

    /** /gas 轮询线程: 2s 一发; 面板关(gasRun=false)即停。 */
    void startGasPoll() {
        gasRun = true;
        gasThr = new Thread(new Runnable() {
            public void run() {
                while (gasRun) {
                    String body = httpGet("http://" + boardIp(PaService.this)
                            + ":8000/gas", 1500);
                    final String[][] rows = parseGas(body);
                    final String err = body == null ? "板子无响应" : null;
                    ui.post(new Runnable() {
                        public void run() {
                            if (gasRun) refreshGasPanel(rows, err);
                        }
                    });
                    try { Thread.sleep(2000); } catch (InterruptedException ig) {}
                }
            }
        });
        gasThr.setDaemon(true);
        gasThr.start();
    }

    static String httpGet(String url, int toMs) {
        java.net.HttpURLConnection c = null;
        try {
            c = (java.net.HttpURLConnection) new java.net.URL(url).openConnection();
            c.setConnectTimeout(toMs);
            c.setReadTimeout(toMs);
            java.io.BufferedReader br = new java.io.BufferedReader(
                    new java.io.InputStreamReader(c.getInputStream()));
            StringBuilder sb = new StringBuilder();
            String l;
            while ((l = br.readLine()) != null) sb.append(l);
            br.close();
            return sb.toString();
        } catch (Exception e) {
            return null;
        } finally {
            if (c != null) try { c.disconnect(); } catch (Exception ig) {}
        }
    }

    /** 解析 /gas JSON -> 每槽 {名称, 数值文本, 状态字符, 状态码}; 解析失败 null */
    static String[][] parseGas(String body) {
        if (body == null) return null;
        try {
            org.json.JSONObject o = new org.json.JSONObject(body);
            if (!o.optBoolean("enabled", false)) return new String[0][];
            org.json.JSONArray arr = o.optJSONArray("sensors");
            if (arr == null) return new String[0][];
            String[][] out = new String[arr.length()][];
            for (int i = 0; i < arr.length(); i++) {
                org.json.JSONObject s = arr.getJSONObject(i);
                String nm = s.optString("gas_name", "");
                if (nm == null || nm.length() == 0)
                    nm = "地址" + s.optInt("addr", i + 1);
                String val = "—", stch = "", stc = "-1";
                if (s.optBoolean("ok", false)) {
                    double conc = s.optDouble("conc", Double.NaN);
                    int dec = s.optInt("dec", 0);
                    int u = s.optInt("unit", 0);
                    String unit = u == 4 ? "%VOL" : u == 2 ? "%LEL" : "ppm";
                    val = (Double.isNaN(conc) ? "—"
                            : String.format(java.util.Locale.US,
                                            "%." + Math.min(3, dec) + "f", conc))
                            + " " + unit;
                    int st = s.optInt("status", -1);
                    stc = String.valueOf(st);
                    stch = st == 1 ? "●" : st == 0 ? "○" : "▲";
                }
                out[i] = new String[]{nm, val, stch, stc};
            }
            return out;
        } catch (Exception e) {
            return null;
        }
    }

    /** 刷 6 行: 名称/数值/状态色(正常绿 低报黄 高报红 预热灰 无数据灰点) */
    void refreshGasPanel(String[][] rows, String err) {
        if (gasPanel == null) return;
        for (int i = 0; i < 6; i++) {
            if (rows != null && i < rows.length) {
                gasRowName[i].setText(rows[i][0]);
                gasRowName[i].setAlpha(1f);
                gasRowVal[i].setText(rows[i][1]);
                gasRowSt[i].setText(rows[i][2]);
                int st = -1;
                try { st = Integer.parseInt(rows[i][3]); } catch (Exception ig) {}
                int col = st == 1 ? PaTheme.GREEN : st == 0 ? PaTheme.TXT2
                        : st == 5 ? PaTheme.WARN : st < 0 ? PaTheme.TXT2 : PaTheme.RED;
                gasRowSt[i].setTextColor(col);
            } else {
                gasRowName[i].setText("— " + (i + 1));
                gasRowName[i].setAlpha(0.45f);
                gasRowVal[i].setText("—");
                gasRowSt[i].setText("");
            }
        }
        gasFoot.setText(err != null ? "⚠ " + err : "每 2s 刷新");
        gasFoot.setTextColor(err != null ? PaTheme.RED : PaTheme.TXT2);
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
        bubbleLp.width = dp(100);          // 花形大钮(FLstyle scallop)
        bubbleLp.height = dp(100);
        bubble = new TextView(this);
        bubble.setText("🎙");
        bubble.setTextSize(52);
        bubble.setGravity(Gravity.CENTER);
        bubble.setBackground(new PaTheme.Scallop(PaTheme.LBLUE));
        dragHandler(bubble, bubble, bubbleLp, "bubble", new Runnable() {
            public void run() { showPanel(); }
        });
    }

    void buildPanel() {
        panelLp = baseLp("panel", 24, 80);
        panelLp.width = dp(240);          // 固定宽: 文字变长/拖动时尺寸不再跳
        panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setBackground(round(PaTheme.LCARD, 16));
        panel.setPadding(dp(14), dp(4), dp(14), dp(12));
        dragHandler(panel, panel, panelLp, "panel", null);   // 空白处也可拖

        // 标题行(拖动区, 加高好抓)
        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.HORIZONTAL);
        top.setGravity(Gravity.CENTER_VERTICAL);
        top.setPadding(0, dp(8), 0, dp(8));
        TextView tt = new TextView(this);
        tt.setText("喊话台");
        tt.setTextColor(PaTheme.LTXT);
        tt.setTextSize(15);
        tt.setTypeface(Typeface.DEFAULT_BOLD);
        tt.setCompoundDrawablesWithIntrinsicBounds(R.drawable.ic_mic, 0, 0, 0);
        tt.setCompoundDrawableTintList(android.content.res.ColorStateList.valueOf(0xFF3B82F6));
        tt.setCompoundDrawablePadding(dp(6));
        top.addView(tt, new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        statusText = new TextView(this);
        statusText.setText("○");
        statusText.setTextColor(PaTheme.LTXT2);
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
        hint.setTextColor(PaTheme.LTXT2);
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
        volText.setTextColor(PaTheme.LTXT);
        volText.setTextSize(26);
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

        // 电平条(交互井轨 + 强调蓝填充)
        levelTrack = new FrameLayout(this);
        levelTrack.setBackground(round(PaTheme.LWELL, 4));
        levelFill = new View(this);
        GradientDrawable fg = round(PaTheme.LBLUE, 4);
        levelFill.setBackground(fg);
        FrameLayout.LayoutParams fp = new FrameLayout.LayoutParams(
                dp(3), FrameLayout.LayoutParams.MATCH_PARENT);
        levelFill.setLayoutParams(fp);
        levelTrack.addView(levelFill);
        LinearLayout.LayoutParams ltp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(5));
        ltp.topMargin = dp(8);
        panel.addView(levelTrack, ltp);

        // 按钮行1: 喊话(唯一主动作=蓝) / 录音(交互井)
        LinearLayout br = new LinearLayout(this);
        br.setOrientation(LinearLayout.HORIZONTAL);
        btnTalk = mkBtn("开始喊话", PaTheme.LBLUE);
        btnRec = mkBtn("开始录音", PaTheme.LWELL);
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
        TextView lib = mkBtn("录音库", PaTheme.LWELL);
        lib.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { showLibrary(); }
        });
        LinearLayout.LayoutParams lpL = new LinearLayout.LayoutParams(0, dp(46), 1);
        lpL.rightMargin = dp(8);
        br2.addView(lib, lpL);
        TextView con = mkBtn("控制台", PaTheme.LWELL);
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
        updBtn.setTextColor(PaTheme.LTXT2);
        updBtn.setTextSize(12);
        updBtn.setPadding(dp(6), dp(8), dp(2), dp(2));
        fr2.addView(updBtn);
        updBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { onUpdateClicked(); }
        });
        panel.addView(fr2, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
    }

    /** 收喊话面板+bubble; 若气体面板还挂着只收自己(服务保留), 全空才停服务 */
    void closePaPanels() {
        try { wm.removeView(panel); } catch (Exception ig) {}
        try { wm.removeView(bubble); } catch (Exception ig) {}
        boolean gasUp = false;
        try { gasUp = gasPanel != null && gasPanel.getParent() != null; } catch (Exception ig) {}
        if (!gasUp) {
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
        t.setTextColor(PaTheme.LTXT2);
        t.setTextSize(16);
        t.setPadding(dp(10), dp(6), dp(2), dp(6));
        return t;
    }

    TextView mkVolBtn(String s) {
        TextView b = new TextView(this);
        b.setText(s);
        b.setTextColor(PaTheme.LTXT);
        b.setTextSize(16);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setGravity(Gravity.CENTER);
        b.setBackground(PaTheme.fill(PaTheme.LWELL, 10));
        return b;
    }

    /** 胶囊按钮: bg 用 PaTheme.LBLUE/WELL/SUB (FLstyle: 字重600, 白/主文字) */
    TextView mkBtn(String text, int bg) {
        TextView b = new TextView(this);
        b.setText(text);
        b.setTextColor(bg == PaTheme.LBLUE ? 0xFFFFFFFF : PaTheme.LTXT);
        b.setTextSize(14);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setGravity(Gravity.CENTER);
        b.setBackground(PaTheme.pill(bg));
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
            // 播放中=蓝色实心胶囊白字(与底栏/分段器同一套选中语言)
            chip.setTextColor(act ? 0xFFFFFFFF : PaTheme.LTXT2);
            chip.setTextSize(13);
            chip.setPadding(dp(14), dp(7), dp(14), dp(7));
            chip.setBackground(round(act ? PaTheme.LBLUE : PaTheme.LWELL, 18));
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
            // 喊话中=红实心(安全可见性, 与手动页急停同一语义; v36 同为红色)
            btnTalk.setBackground(PaTheme.pill(PaTheme.LRED_BG));
            hint.setText("● 喊话中… 对麦克风说话");
            hint.setTextColor(PaTheme.LRED);
        } else if (mode == M_REC) {
            btnRec.setText("完成录音");
            hint.setText("● 录音中… 点「完成录音」保存(最长60秒)");
            hint.setTextColor(PaTheme.LBROWN);
        } else if (mode == M_PLAY) {
            hint.setText("▶ 板子播放中: " + playingName + " (再点一次停止)");
            hint.setTextColor(PaTheme.LGREEN);
        } else if (mode == M_AUD) {
            hint.setText("♪ 试听中: " + playingName + " (再点一次停止)");
            hint.setTextColor(PaTheme.LBLUE);
        } else {
            btnTalk.setText("开始喊话");
            btnTalk.setBackground(PaTheme.pill(PaTheme.LBLUE));
            btnRec.setText("开始录音");
            hint.setText("○ 未在喊话");
            hint.setTextColor(PaTheme.LTXT2);
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
            InetAddress dst = InetAddress.getByName(dstIp());
            ctrl("STOP");                          // v55: 喊话前显式停预设播放(板端 TALK1 亦停, 双保险)
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
        libSub.clear();                         // 重建对话框, 旧行引用作废
        ScrollView sc = new ScrollView(this);
        LinearLayout list = new LinearLayout(this);
        list.setOrientation(LinearLayout.VERTICAL);
        list.setPadding(dp(4), dp(4), dp(4), dp(4));
        sc.addView(list);

        // 分组列表范式(FlClash): 分组标题在卡外上方, 一卡多行, 1px 内缩分隔线
        list.addView(groupTitle("音频预设", R.drawable.ic_note));
        LinearLayout cardAudio = new LinearLayout(this);
        cardAudio.setOrientation(LinearLayout.VERTICAL);
        cardAudio.setBackground(round(PaTheme.LCARD, 16));
        list.addView(cardAudio, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        boolean hasRow = false;
        for (int i = 0; i < presets.length(); i++) {
            JSONObject o = presets.optJSONObject(i);
            if (o == null) continue;
            final int idx = i;
            if (hasRow) cardAudio.addView(divider());
            hasRow = true;
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.setGravity(Gravity.CENTER_VERTICAL);
            row.setPadding(dp(12), dp(10), dp(12), dp(10));
            cardAudio.addView(row, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

            // 行首图标井(FlClash 缩略图范式: 圆角方井+单色线性图标)
            android.widget.ImageView iv = new android.widget.ImageView(this);
            iv.setImageResource(R.drawable.ic_note);
            iv.setColorFilter(0xFF6B7280);
            iv.setBackground(round(PaTheme.LWELL, 12));
            int ivPad = dp(8);
            iv.setPadding(ivPad, ivPad, ivPad, ivPad);
            row.addView(iv);

            int bst = o.optInt("bst", 0);
            boolean isUp = uploading.contains(o.optString("name"));
            String stTxt = isUp ? "↻ 同步中…"
                    : bst == 1 ? "✓ 板上已有" : bst == 2 ? "⚠ 未同步" : "↑ 未同步";
            // 状态色: 已同步=绿 / 未同步=红 / 同步中=次文字(转圈中不算告警)
            int stColor = isUp ? PaTheme.LTXT2 : bst == 1 ? PaTheme.LGREEN : PaTheme.LRED;
            // 两行式条目(11-04: 旧"名+(时长 状态)"单行被四个动作钮挤成四行竖排):
            // 行1 纯名称(单行省略), 行2 时长·状态, 动作钮仍贴右缘垂直居中
            LinearLayout nameCol = new LinearLayout(this);
            nameCol.setOrientation(LinearLayout.VERTICAL);
            TextView nm1 = new TextView(this);
            nm1.setText(o.optString("name"));
            nm1.setTextColor(PaTheme.LTXT);
            nm1.setTextSize(15);
            nm1.setTypeface(Typeface.DEFAULT_BOLD);
            nm1.setSingleLine(true);
            nm1.setEllipsize(android.text.TextUtils.TruncateAt.END);
            nameCol.addView(nm1);
            String dur = String.format(java.util.Locale.US, "%.1fs", o.optDouble("dur", 0));
            String sub = dur + " · " + stTxt;
            android.text.SpannableString ss = new android.text.SpannableString(sub);
            ss.setSpan(new android.text.style.ForegroundColorSpan(PaTheme.LTXT2),
                    0, dur.length(), android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            ss.setSpan(new android.text.style.ForegroundColorSpan(stColor),
                    dur.length(), sub.length(), android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            TextView sub1 = new TextView(this);
            sub1.setText(ss);
            sub1.setTextSize(12);
            sub1.setSingleLine(true);
            nameCol.addView(sub1);
            libSub.put(o.optString("name"), sub1);
            LinearLayout.LayoutParams np = new LinearLayout.LayoutParams(0,
                    LinearLayout.LayoutParams.WRAP_CONTENT, 1);
            np.leftMargin = dp(10);
            row.addView(nameCol, np);
            row.setOnClickListener(new View.OnClickListener() {
                public void onClick(View v) { playPreset(idx); }
            });
            if (!o.optString("file").isEmpty()) {
                TextView up = new TextView(this);
                up.setText("上传");
                up.setTextColor(PaTheme.LTXT2);
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
                aud.setTextColor(PaTheme.LTXT2);
                aud.setTextSize(14);
                aud.setPadding(dp(12), dp(8), dp(6), dp(8));
                row.addView(aud);
                aud.setOnClickListener(new View.OnClickListener() {
                    public void onClick(View v) { toggleAud(idx); }
                });
            }
            TextView rn = new TextView(this);
            rn.setText("改名");
            rn.setTextColor(PaTheme.LTXT2);
            rn.setTextSize(14);
            rn.setPadding(dp(8), dp(8), dp(6), dp(8));
            row.addView(rn);
            rn.setOnClickListener(new View.OnClickListener() {
                public void onClick(View v) { renamePreset(idx); }
            });
            TextView del = new TextView(this);
            del.setText("删除");
            del.setTextColor(PaTheme.LRED);
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

        list.addView(groupTitle("添加与工具", R.drawable.ic_add));
        LinearLayout cardTools = new LinearLayout(this);
        cardTools.setOrientation(LinearLayout.VERTICAL);
        cardTools.setBackground(round(PaTheme.LCARD, 16));
        list.addView(cardTools, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        cardTools.addView(toolRow(R.drawable.ic_add, "从文件添加 (mp3 / m4a / wav)",
                new View.OnClickListener() {
                    public void onClick(View v) {
                        Intent i = new Intent(PaService.this, ImportActivity.class);
                        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                        startActivity(i);
                    }
                }));
        cardTools.addView(divider());
        cardTools.addView(toolRow(R.drawable.ic_folder, "扫描 Download 目录",
                new View.OnClickListener() {
                    public void onClick(View v) { scanDownload(); }
                }));
        cardTools.addView(divider());
        cardTools.addView(toolRow(R.drawable.ic_edit, "文字转语音",
                new View.OnClickListener() {
                    public void onClick(View v) { ttsDialog(); }
                }));
        cardTools.addView(divider());
        cardTools.addView(toolRow(R.drawable.ic_list, "传输日志 (对讲丢包/上传)",
                new View.OnClickListener() {
                    public void onClick(View v) { showLogs(); }
                }));

        // 自定义深色对话框(替代系统 AlertDialog 的主题外框; 悬浮窗服务弹窗必须设 overlayType)
        final android.app.Dialog d = new android.app.Dialog(this);
        d.requestWindowFeature(android.view.Window.FEATURE_NO_TITLE);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setBackground(round(PaTheme.LCARD, 18));
        box.setPadding(dp(18), dp(14), dp(18), dp(12));
        LinearLayout ltRow = new LinearLayout(this);            // v54: 标题行右侧一键上传
        ltRow.setOrientation(LinearLayout.HORIZONTAL);
        ltRow.setGravity(Gravity.CENTER_VERTICAL);
        TextView lt = new TextView(this);
        lt.setText("录音库");
        lt.setTextColor(PaTheme.LTXT);
        lt.setTextSize(16);
        lt.setTypeface(Typeface.DEFAULT_BOLD);
        ltRow.addView(lt, new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        TextView upAll = mkGhost("全部上传");
        upAll.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { uploadAll(); }
        });
        ltRow.addView(upAll);
        box.addView(ltRow);
        TextView ls = new TextView(this);
        ls.setText("点条目=未同步先同步, 同步后点即播 · ✓=板上已有");
        ls.setTextColor(PaTheme.LTXT2);
        ls.setTextSize(12);
        ls.setPadding(0, dp(2), 0, dp(8));
        box.addView(ls);
        libSubText = ls;
        box.addView(sc, new LinearLayout.LayoutParams(   // weight=1: 列表占余高, 关闭/传输日志恒在屏内
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        TextView close = new TextView(this);
        close.setText("关 闭");
        close.setTextColor(PaTheme.LTXT2);
        close.setTextSize(14);
        close.setGravity(Gravity.CENTER);
        close.setPadding(0, dp(10), 0, dp(4));
        close.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { d.dismiss(); }
        });
        box.addView(close);
        d.setContentView(box);
        android.view.Window win = d.getWindow();
        if (win != null) {
            win.setType(overlayType());
            win.setBackgroundDrawable(new android.graphics.drawable.ColorDrawable(0x00000000));
            win.setLayout(dp(430), (int) (getResources().getDisplayMetrics().heightPixels
                    * 0.86));   // 限高 86%: 330->430dp 修名称列, 限高修底部按钮被挡
        }
        libDialog = d;
        d.show();
        refreshUpdBtn();
    }

    android.app.Dialog libDialog;
    android.widget.TextView libSubText;   // 库容量说明行(同步完成后常刷)
    final java.util.HashMap<String, android.widget.TextView> libSub =
            new java.util.HashMap<String, android.widget.TextView>();   // 名字->行状态行(v54 百分比回帖)

    /** 分组列表: 卡外分组标题(小图标+次文字) */
    TextView groupTitle(String s, int icon) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextColor(PaTheme.LTXT2);
        t.setTextSize(13);
        t.setCompoundDrawablesWithIntrinsicBounds(icon, 0, 0, 0);
        t.setCompoundDrawableTintList(android.content.res.ColorStateList.valueOf(0xFF6B7280));
        t.setCompoundDrawablePadding(dp(6));
        t.setPadding(dp(2), dp(14), dp(2), dp(8));
        return t;
    }

    /** 分组列表: 行间 1px 内缩分隔线 */
    View divider() {
        View v = new View(this);
        v.setBackground(new android.graphics.drawable.ColorDrawable(PaTheme.LDIV));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(1));
        lp.leftMargin = dp(12);
        lp.rightMargin = dp(12);
        v.setLayoutParams(lp);
        return v;
    }

    /** 分组列表: 工具行(图标井 + 标签 + ›) */
    LinearLayout toolRow(int icon, String label, View.OnClickListener ck) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(dp(12), dp(12), dp(12), dp(12));
        android.widget.ImageView iv = new android.widget.ImageView(this);
        iv.setImageResource(icon);
        iv.setColorFilter(0xFF6B7280);
        iv.setBackground(round(PaTheme.LWELL, 12));
        int pad = dp(8);
        iv.setPadding(pad, pad, pad, pad);
        row.addView(iv);
        TextView t = new TextView(this);
        t.setText(label);
        t.setTextColor(PaTheme.LTXT2);
        t.setTextSize(14);
        t.setPadding(dp(10), 0, 0, 0);
        row.addView(t, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        TextView chev = new TextView(this);
        chev.setText("›");
        chev.setTextColor(PaTheme.LTXT2);
        chev.setTextSize(16);
        row.addView(chev);
        row.setOnClickListener(ck);
        return row;
    }

    void refreshUpdBtn() {
        if (updBtn == null) return;
        if (latestVer > APP_VERSION) {
            updBtn.setText("发现新版 v" + latestVer + " · 点击更新");
            updBtn.setTextColor(PaTheme.LGREEN);
            updBtn.setTypeface(Typeface.DEFAULT_BOLD);
        } else {
            updBtn.setText("检查更新");
            updBtn.setTextColor(PaTheme.LTXT2);
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
        if (bst != 1) {                    // 未同步/不符: 只上传, 传完不播(11-04 用户); 传完点一下即播
            toast(bst == 2 ? "校验不符, 重新同步, 传完再点一下即播" : "先同步到板子, 传完再点一下即播");
            enqueueUpload(name, false);
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
            rateBtn.setTextColor(talkRate >= 16000 ? PaTheme.LTXT2 : PaTheme.LBROWN);
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
            s.connect(InetAddress.getByName(dstIp()), CTRL_PORT);
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
                if (libDialog != null && libSubText != null)   // 库说明行常显板上容量(v37: setTitle 在 NO_TITLE 对话框上无效, 改更新说明行)
                    libSubText.setText("板上 " + board.size() + "/" + BOARD_MAX_FILES
                            + " 条 · 点条目=未同步先同步 · ✓=板上已有");
                addLog("同步完成: 板上 " + board.size() + " 条音频");
            }});
        }});
    }

    /** 完整性优先的上传: 板端 crc 校验通过(DONE)才算数; 传完只标记, 绝不自动播(11-04 用户:
     *  传完了就传完了, 等我点才放)。thenPlay 形参保留兼容旧调用, 不再产生播放。 */
    void enqueueUpload(final String name, final boolean thenPlay) {
        if (uploading.contains(name)) return;
        if (upPool.isShutdown()) return;   // 录音收尾/导入/TTS 迟到路径: 服务已销毁别再炸进程
        uploading.add(name);
        if (libDialog != null) showLibrary();   // 11-04: 旧版入队后无任何反馈, 像"点了没用"
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
                toast("同步 " + name + (fok ? " 完成" : " 失败: " + fmsg));
                if (fok) markBst(name, 1); else markBst(name, 2);
                savePresets();
                buildChips();                      // 面板胶囊行跟着刷新(对话框关着也能看到)
                renderMode();                      // 清掉 hint 里的"同步中 xx%"
                if (libDialog != null) showLibrary();   // markBst 之后重建, 行内直接显示新状态
            }});
        }});
    }

    /** HTTP 直传 /paup(v53): 旧 UDP 15003 分块协议弃用——大文件传输中 板->手机 ACK 流
     *  会被 G12 WiFi 断掉(43KB 正常/4.3MB 必卡死), TCP 自带重传根治; 板端端点语义不变。 */
    boolean uploadToBoard(String name, byte[] data) {
        java.net.HttpURLConnection c = null;
        try {
            String nm = java.net.URLEncoder.encode(name, "UTF-8");
            c = (java.net.HttpURLConnection) new java.net.URL(
                    "http://" + dstIp() + ":8000/paup?name=" + nm).openConnection();
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setConnectTimeout(4000);
            // 读超时要盖住 body 尾巴在慢链路的爬行时间(狗内网 AP 实测 ~41KB/s,
            // 4.3MB 尾部曾晚到 >20s 被误判失败, 板端其实收到了)
            c.setReadTimeout(180000);
            c.setFixedLengthStreamingMode(data.length);   // 流式, 不在内存再攒一份
            java.io.OutputStream os = c.getOutputStream();
            int done = 0, lastPct = -4;
            while (done < data.length) {               // v54: 分片写, 行内回帖百分比
                int n = Math.min(32768, data.length - done);
                os.write(data, done, n);
                done += n;
                int pct = done * 100 / data.length;
                if (pct >= lastPct + 4) {
                    lastPct = pct;
                    final int fp = pct;
                    ui.post(new Runnable() { public void run() {
                        android.widget.TextView tv = libSub.get(name);
                        if (tv != null) tv.setText("↻ 同步中 " + fp + "%");
                    }});
                }
            }
            os.flush();
            os.close();
            int code = c.getResponseCode();
            if (code != 200) { lastUpErr = "HTTP " + code; return false; }
            java.io.BufferedReader br = new java.io.BufferedReader(
                    new java.io.InputStreamReader(c.getInputStream(), "UTF-8"));
            String r = br.readLine();
            br.close();
            if (r != null && r.startsWith("DONE")) { lastUpErr = ""; return true; }
            lastUpErr = (r == null || r.length() == 0) ? "板子无应答" : r;
            return false;
        } catch (Exception e) {
            String m = String.valueOf(e);
            lastUpErr = m.length() > 60 ? m.substring(0, 60) : m;
            return false;
        } finally {
            if (c != null) c.disconnect();
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
        tv.setTextColor(PaTheme.LTXT2);
        tv.setTypeface(Typeface.MONOSPACE);
        tv.setPadding(dp(12), dp(10), dp(12), dp(10));
        ScrollView sc2 = new ScrollView(this);
        sc2.addView(tv);
        // 自定义深色对话框(与录音库同语言; 悬浮窗服务弹窗必须 overlayType)
        final android.app.Dialog d = new android.app.Dialog(this);
        d.requestWindowFeature(android.view.Window.FEATURE_NO_TITLE);
        LinearLayout box2 = new LinearLayout(this);
        box2.setOrientation(LinearLayout.VERTICAL);
        box2.setBackground(round(PaTheme.LCARD, 18));
        box2.setPadding(dp(18), dp(14), dp(18), dp(12));
        TextView lt2 = new TextView(this);
        lt2.setText("传输日志");
        lt2.setTextColor(PaTheme.LTXT);
        lt2.setTextSize(16);
        lt2.setTypeface(Typeface.DEFAULT_BOLD);
        box2.addView(lt2);
        TextView ls2 = new TextView(this);
        ls2.setText("对讲丢包/上传, 最近60条");
        ls2.setTextColor(PaTheme.LTXT2);
        ls2.setTextSize(12);
        ls2.setPadding(0, dp(2), 0, dp(8));
        box2.addView(ls2);
        box2.addView(sc2, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(300)));
        LinearLayout btns2 = new LinearLayout(this);
        btns2.setOrientation(LinearLayout.HORIZONTAL);
        TextView clr = new TextView(this);
        clr.setText("清 空");
        clr.setTextColor(PaTheme.LRED);
        clr.setTextSize(14);
        clr.setTypeface(Typeface.DEFAULT_BOLD);
        clr.setGravity(Gravity.CENTER);
        clr.setPadding(0, dp(10), 0, dp(4));
        clr.setBackground(PaTheme.pill(PaTheme.LWELL));
        clr.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                synchronized (logLock) { logs.clear(); }
                tv.setText("(还没有日志)");
            }
        });
        btns2.addView(clr, new LinearLayout.LayoutParams(0, dp(42), 1));
        TextView cls = new TextView(this);
        cls.setText("关 闭");
        cls.setTextColor(PaTheme.LTXT);
        cls.setTextSize(14);
        cls.setTypeface(Typeface.DEFAULT_BOLD);
        cls.setGravity(Gravity.CENTER);
        cls.setPadding(0, dp(10), 0, dp(4));
        cls.setBackground(PaTheme.pill(PaTheme.LBLUE));
        LinearLayout.LayoutParams clsp = new LinearLayout.LayoutParams(0, dp(42), 1);
        clsp.leftMargin = dp(10);
        btns2.addView(cls, clsp);
        cls.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { d.dismiss(); }
        });
        LinearLayout.LayoutParams bp2 = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        bp2.topMargin = dp(12);
        box2.addView(btns2, bp2);
        d.setContentView(box2);
        android.view.Window win2 = d.getWindow();
        if (win2 != null) {
            win2.setType(overlayType());
            win2.setBackgroundDrawable(new android.graphics.drawable.ColorDrawable(0x00000000));
            win2.setLayout(dp(330), -2);
        }
        d.show();
    }

    // ================= 探活 + 音量 =================

    String ctrl(String cmd) {
        DatagramSocket s = null;
        try {
            s = new DatagramSocket();
            s.setSoTimeout(1500);
            s.connect(InetAddress.getByName(dstIp()), CTRL_PORT);
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
                if (ok && mode != M_LIVE && mode != M_REC && mode != M_AUD) {
                    final String st = ctrl("STAT?");   // v55: 面板重开/服务重建后播放态对账
                    if (st != null && st.startsWith("OK STAT ")) {
                        final String nm = st.substring(8).trim();
                        ui.post(new Runnable() { public void run() {
                            if (mode == M_LIVE || mode == M_REC || mode == M_AUD) return;
                            boolean playing = !nm.isEmpty() && !nm.equals("idle");
                            if (playing && mode != M_PLAY) {
                                playingName = nm;
                                setMode(M_PLAY);
                                if (playFinish != null) ui.removeCallbacks(playFinish);
                                final long ms = (long) ((findDur(nm) > 0 ? findDur(nm) * 1000 : 30000) + 900);
                                playFinish = new Runnable() { public void run() {
                                    if (mode == M_PLAY && nm.equals(playingName)) setMode(M_IDLE);
                                }};
                                ui.postDelayed(playFinish, ms);   // 时长按整段估(重开场景无起点)
                            } else if (!playing && mode == M_PLAY) {
                                if (playFinish != null) { ui.removeCallbacks(playFinish); playFinish = null; }
                                setMode(M_IDLE);
                            }
                        }});
                    }
                }
                ui.post(new Runnable() { public void run() {
                    statusText.setText(ok ? "●" : "○");
                    statusText.setTextColor(ok ? PaTheme.LGREEN : PaTheme.LTXT2);
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
                        "http://" + dstIp() + ":" + PAGE_PORT + "/version.json").openConnection();
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
            updBtn.setTextColor(PaTheme.LBLUE);
            updBtn.setText("下载中…");
        }
        new Thread() { public void run() {
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(
                        "http://" + dstIp() + ":" + PAGE_PORT + "/PaSend.apk").openConnection();
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
                        updBtn.setTextColor(PaTheme.LRED);
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
            toast("弹安装失败, 用浏览器开 http://" + dstIp() + ":" + PAGE_PORT + " 手动装");
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
