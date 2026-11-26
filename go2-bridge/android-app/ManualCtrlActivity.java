package com.wjl.pasend;

import android.app.Activity;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;

/**
 * 手动控制 (v32): 屏幕摇杆 + 按键 -> UDP 15010 通道数组 -> 板端 netrc -> 狗
 * 通道语义与实体遥控 SBUS 完全一致, 桥的解锁边沿/失控保护逻辑原样复用:
 *   ch0=转向 ch1=前后 ch4=A(解锁,按键翻转=天然边沿) ch6=C(站/趴) ch9=挡位
 * 退出/切后台 -> 停发 -> 板端 300ms 内回退 SBUS 线; 无 SBUS 则走失控保护站定
 */
public class ManualCtrlActivity extends Activity {

    static final int NETRC_PORT = 15010;   // 与板端 config.NETRC_PORT 一致

    volatile boolean armed = false;        // A 键
    volatile boolean walk = false;         // C 键(上=站立/行走)
    volatile int gear = 1;                 // 挡位 1经济/2自由/3奔跑
    volatile float joyX = 0f, joyY = 0f;   // 左杆: Y=前后 X=左右平移
    volatile float joy1X = 0f;             // 右杆X(转向), Y 不用
    volatile boolean sending = false;
    int seq = 0;

    TextView status, boardDot;
    TextView armBtn, walkBtn;
    TextView[] gearBtns = new TextView[3];
    BatteryView batt;                      // 电池图标(探活每 3s 随 RC 应答刷新)
    volatile int soc = -1;                 // 狗电量 %(RC 应答第 4 字段; -1=未知/不新鲜)
    volatile long lastUserActT = 0L;       // 最近一次用户按键(状态校正 1.5s 宽限窗)
    Handler ui = new Handler();
    DatagramSocket sock;
    volatile Thread sender, prober;   // 发送线程自校验跨线程读, 需可见性
    JoyView joyL, joyR;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        // v41 浅色壳: 去系统标题栏, 状态栏融入浅底
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        getWindow().setStatusBarColor(PaTheme.LBG);
        getWindow().setNavigationBarColor(PaTheme.LBG);
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        // 开着控制页不许熄屏(熄屏=停发=狗失控保护, 和控制台同款教训)
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        buildUi();
    }

    @Override
    protected void onResume() {
        super.onResume();
        startSender();
        startProber();
    }

    @Override
    protected void onPause() {
        sending = false;   // 停发: 板端 300ms 后回退 SBUS, 没线则失控保护
        if (sender != null) sender.interrupt();
        if (prober != null) prober.interrupt();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        sending = false;
        try { if (sock != null) sock.close(); } catch (Exception ig) {}
        super.onDestroy();
    }

    // ============ 发送: 50ms(20Hz) 一包全通道 ============
    // 必须 20Hz: 链路固有 15~28% 丢包, 10Hz 时连丢 3 包(300ms)就破板端 FS_STOP_S=0.25s
    // 失控线, 狗会在行驶中频繁 StopMove 抽搐; 20Hz 要连丢 5 包才越界(概率~0.1%)

    void startSender() {
        if (sending) return;
        sending = true;
        if (sock == null) {
            try { sock = new DatagramSocket(); } catch (Exception e) { sock = null; }
        }
        sender = new Thread(new Runnable() { public void run() {
            final Thread me = Thread.currentThread();
            // v47 接管继承: 首包前先问桥当前真实 RC 状态, 按它初始化解锁/姿态/挡位,
            // 避免默认"上锁+趴"被桥当成 A/C 翻拨边沿 -> 狗被按趴; 查不到(离线/超时)保持安全默认
            // 超时预算必须大于桥侧: pa_recv 的 RC 应答要读一次 HTTP /state(超时 1.0s),
            // 客户端 700ms 会在板子稍慢时判"未知"-> 回退上锁 -> 把已解锁的狗按趴。
            // 1600ms x 2 次; 两次都失败才按上锁默认(离线场景)。
            for (int attempt = 0; attempt < 2; attempt++) {
                if (!sending || sender != me) break;
                DatagramSocket q = null;
                boolean got = false;
                try {
                    q = new DatagramSocket();
                    q.setSoTimeout(1600);
                    byte[] ob = "RC".getBytes();
                    q.send(new DatagramPacket(ob, ob.length,
                            InetAddress.getByName(PaService.boardIp(ManualCtrlActivity.this)),
                            PaService.CTRL_PORT));
                    byte[] rb = new byte[96];
                    DatagramPacket rp = new DatagramPacket(rb, rb.length);
                    q.receive(rp);
                    String[] sp = new String(rb, 0, rp.getLength(), "UTF-8").trim().split(" ");
                    if (sp.length >= 3 && sp[0].equals("RC")) {
                        synchronized (ManualCtrlActivity.this) {
                            armed = sp[1].equals("1");
                            int m = Integer.parseInt(sp[2]);
                            walk = (m == 1 || m == 2);   // 站/走 = C 上位; 趴/坐 = C 下位
                            if (sp.length > 3 && !"None".equals(sp[3])) {
                                int g = Integer.parseInt(sp[3]);
                                if (g >= 1 && g <= 3) gear = g;
                            }
                            soc = parseSoc(sp);       // 电池电量(新鲜才收, 首查与探活同源)
                        }
                        got = true;
                        ui.post(new Runnable() { public void run() { refreshBtns(); } });
                    }
                } catch (Exception ig) {
                    // 单次失败: 重试; 两次都失败才落到上锁默认(与旧行为一致)
                } finally {
                    if (q != null) q.close();
                }
                if (got) break;
            }
            while (sending && sender == me) {
                boolean a, w; int g, s; float jx, jy, j1x;
                synchronized (ManualCtrlActivity.this) {
                    a = armed; w = walk; g = gear; jx = joyX; jy = joyY; j1x = joy1X; s = ++seq;
                }
                StringBuilder js = new StringBuilder(140);
                js.append("{\"seq\":").append(s).append(",\"ch\":[");
                for (int i = 0; i < 16; i++) {
                    int v = 1002;
                    if (i == 0) v = 1002 + (int) (j1x * 720);           // 转向: 右杆X, 右推值增(ch0 右+)
                    else if (i == 1) v = 1002 - (int) (jy * 720);       // 前后: 左杆Y, 实体标定"左竖下+", 上推=值减!
                    else if (i == 3) v = 1002 + (int) (jx * 720);       // 侧移: 左杆X, 右推值增
                    else if (i == 4) v = a ? 1722 : 1002;               // A 解锁位
                    else if (i == 6) v = w ? 1722 : 282;                // C 上=站/走
                    else if (i == 9) v = (g == 1) ? 282 : (g == 2 ? 1002 : 1722);
                    js.append(i == 0 ? "" : ",").append(v);
                }
                js.append("]}");
                try {
                    byte[] ob = js.toString().getBytes("UTF-8");
                    if (sock != null) sock.send(new DatagramPacket(ob, ob.length,
                            InetAddress.getByName(PaService.boardIp(ManualCtrlActivity.this)), NETRC_PORT));
                } catch (Exception e) {}
                try { Thread.sleep(50); } catch (InterruptedException e) { break; }
            }
        }});
        sender.setDaemon(true);
        sender.start();
        tickStatus();
    }

    /** 板子在线探活: 3s 一次 HELLO + RC(读桥真实状态, 防止"UI解锁了桥其实锁着"的盲区) */
    void startProber() {
        prober = new Thread(new Runnable() { public void run() {
            while (sending) {
                String r = "", rc = "";
                DatagramSocket s = null;
                try {
                    s = new DatagramSocket();
                    s.setSoTimeout(1500);
                    byte[] ob = "HELLO".getBytes();
                    s.send(new DatagramPacket(ob, ob.length,
                            InetAddress.getByName(PaService.boardIp(ManualCtrlActivity.this)), PaService.CTRL_PORT));
                    byte[] rb = new byte[96];
                    DatagramPacket rp = new DatagramPacket(rb, rb.length);
                    s.receive(rp);
                    r = new String(rb, 0, rp.getLength(), "UTF-8");
                    // 第二问: 桥的真实 RC 状态
                    byte[] ob2 = "RC".getBytes();
                    s.send(new DatagramPacket(ob2, ob2.length,
                            InetAddress.getByName(PaService.boardIp(ManualCtrlActivity.this)), PaService.CTRL_PORT));
                    DatagramPacket rp2 = new DatagramPacket(rb, rb.length);
                    try { s.receive(rp2); rc = new String(rb, 0, rp2.getLength(), "UTF-8"); } catch (Exception ig) {}
                } catch (Exception e) {
                } finally { try { if (s != null) s.close(); } catch (Exception ig) {} }
                final boolean ok = r.startsWith("OK");
                final String boardRc = rc;   // "RC 1 2 1 soc fresh" = 解锁/行走/挡1/电量/新鲜
                ui.post(new Runnable() { public void run() {
                    if (boardDot != null) {
                        // 在线徽章(FlClash "质量优"范式): 暗绿胶囊+绿字; 离线=次表面胶囊+红字
                        boardDot.setText(ok ? ("● 在线" + fmtRc(boardRc)) : "● 板子无响应");
                        boardDot.setTextColor(ok ? PaTheme.LGREEN : PaTheme.LRED);
                        boardDot.setBackground(PaTheme.pill(ok ? PaTheme.LGREENBG : PaTheme.LWELL));
                    }
                    applyBoardRc(boardRc);   // 48 起: 桥真实状态回灌(断电重启自动跟上)
                }});
                try { Thread.sleep(3000); } catch (InterruptedException e) { break; }
            }
        }});
        prober.setDaemon(true);
        prober.start();
    }

    String fmtRc(String rc) {
        try {
            String[] sp = rc.trim().split(" ");
            if (sp.length >= 3 && sp[0].equals("RC")) {
                String s = " · 桥:" + ("1".equals(sp[1]) ? "解锁" : "上锁")
                        + "/" + new String[]{"趴", "站", "走", "坐"}[Integer.parseInt(sp[2])];
                if (sp.length > 3 && !"None".equals(sp[3])) s += "/挡" + sp[3];
                return s;
            }
        } catch (Exception ig) {}
        return "";
    }

    /** RC 应答第 4/5 字段 -> 电量%(0..100; 不新鲜/非法 = -1)(v48; 首查块与探活回灌共用) */
    static int parseSoc(String[] sp) {
        if (sp.length > 4) {
            try {
                int v = Integer.parseInt(sp[4]);
                if (v >= 0 && v <= 100 && sp.length > 5 && sp[5].equals("1")) return v;
            } catch (Exception ig) {}
        }
        return -1;
    }

    /** 探活拿到的桥真实 RC 状态回灌(48 起): 校正本地解锁/姿态/挡位与发送电平 ——
     *  狗断电重启/被遥控器抢控后页面自动跟上, 不再停留在离开时刻的画面。
     *  用户按键后 1.5s 内不校正(防在途指令被在途旧查询回弹); 电量无论如何都刷新。 */
    void applyBoardRc(String rc) {
        try {
            String[] sp = rc.trim().split(" ");
            if (sp.length < 3 || !sp[0].equals("RC")) return;
            int ns = parseSoc(sp);
            if (ns != soc) { soc = ns; if (batt != null) batt.invalidate(); }
            boolean bArm = sp[1].equals("1");
            int m = Integer.parseInt(sp[2]);
            boolean bWalk = (m == 1 || m == 2);
            int bGear = -1;
            if (sp.length > 3 && !"None".equals(sp[3])) {
                int g = Integer.parseInt(sp[3]);
                if (g >= 1 && g <= 3) bGear = g;
            }
            boolean changed = false;
            synchronized (this) {
                if (System.currentTimeMillis() - lastUserActT < 1500) return;
                if (bArm != armed) { armed = bArm; changed = true; }
                if (bWalk != walk) { walk = bWalk; changed = true; }
                if (bGear > 0 && bGear != gear) { gear = bGear; changed = true; }
            }
            if (changed) refreshBtns();
        } catch (Exception ig) {}
    }

    /** 电池图标(48): 横椭圆外壳+内部填充+右侧百分比; soc<0=未知(灰壳"--")。
     *  颜色随电量: <20 红 / <40 黄 / 其余绿; 数据来自探活 RC 应答第 4/5 字段。 */
    class BatteryView extends View {
        BatteryView(android.content.Context c) { super(c); }
        @Override protected void onDraw(android.graphics.Canvas cv) {
            final float D = getResources().getDisplayMetrics().density;
            android.graphics.Paint p = new android.graphics.Paint(Paint.ANTI_ALIAS_FLAG);
            int w = getWidth(), h = getHeight();
            float pad = 2 * D, bodyW = w - 34 * D, bodyH = h - 5 * D;
            android.graphics.RectF body = new android.graphics.RectF(0, pad, bodyW, pad + bodyH);
            int s = soc;
            int shell = (s < 0) ? 0xFFD1D5DB : PaTheme.LDIV;
            int fill = (s < 0) ? 0xFF9CA3AF
                     : (s < 20 ? PaTheme.LRED : s < 40 ? 0xFFF59E0B : PaTheme.LGREEN);
            p.setStyle(android.graphics.Paint.Style.STROKE);
            p.setStrokeWidth(1.5f * D); p.setColor(shell);
            cv.drawRoundRect(body, bodyH / 2f, bodyH / 2f, p);
            p.setStyle(android.graphics.Paint.Style.FILL); p.setColor(shell);
            cv.drawRoundRect(new android.graphics.RectF(body.right + D, h / 2f - 3.5f * D,
                    body.right + D + 3.5f * D, h / 2f + 3.5f * D), 2 * D, 2 * D, p);
            if (s >= 0) {
                float frac = Math.max(0f, Math.min(1f, s / 100f));
                float iw = Math.max((bodyW - 6 * D) * frac, 4 * D);
                p.setColor(fill);
                cv.drawRoundRect(new android.graphics.RectF(body.left + 3 * D, body.top + 3 * D,
                        body.left + 3 * D + iw, body.bottom - 3 * D),
                        (bodyH - 6 * D) / 2f, (bodyH - 6 * D) / 2f, p);
            }
            p.setColor(s < 0 ? 0xFF9CA3AF : (s < 20 ? PaTheme.LRED : PaTheme.LTXT));
            p.setTextSize(12 * D);
            cv.drawText(s < 0 ? "--" : s + "%", bodyW + 6 * D, h / 2f + 4 * D, p);
        }
    }

    void tickStatus() {
        ui.postDelayed(new Runnable() { public void run() {
            if (status != null) {
                status.setText(String.format("seq %d · %s · 挡%d · 左杆 %.2f/%.2f · 右杆 %.2f",
                        seq, armed ? "已解锁" : "上锁", gear, joyX, joyY, joy1X));
            }
            if (sending) tickStatus();
        }}, 500);
    }

    // ============ UI ============

    int dp(int v) { return Math.round(v * getResources().getDisplayMetrics().density); }

    /** 摇杆+底部说明的竖排容器 */
    LinearLayout captioned(JoyView j, String cap) {
        LinearLayout w = new LinearLayout(this);
        w.setOrientation(LinearLayout.VERTICAL);
        w.addView(j, new LinearLayout.LayoutParams(-1, 0, 1f));
        TextView c = new TextView(this);
        c.setText(cap);
        c.setTextSize(11);
        c.setTextColor(PaTheme.LTXT2);
        c.setGravity(Gravity.CENTER);
        c.setPadding(0, dp(4), 0, dp(2));
        w.addView(c, new LinearLayout.LayoutParams(-1, -2));
        return w;
    }

    TextView btn(String text, int bg, int fg) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextSize(17);
        t.setTypeface(null, android.graphics.Typeface.BOLD);
        t.setTextColor(fg);
        t.setGravity(Gravity.CENTER);
        GradientDrawable d = new GradientDrawable();
        d.setColor(bg);
        d.setCornerRadius(dp(20));
        t.setBackground(d);
        t.setPadding(dp(8), dp(10), dp(8), dp(10));
        return t;
    }

    void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(PaTheme.LBG);

        // 顶栏
        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.HORIZONTAL);
        top.setGravity(Gravity.CENTER_VERTICAL);
        top.setPadding(dp(12), dp(10), dp(12), dp(4));
        TextView title = new TextView(this);
        title.setText("手动控制");
        title.setTextSize(18);
        title.setTypeface(null, android.graphics.Typeface.BOLD);
        title.setTextColor(PaTheme.LTXT);
        title.setCompoundDrawablesWithIntrinsicBounds(R.drawable.ic_pad, 0, 0, 0);
        title.setCompoundDrawableTintList(android.content.res.ColorStateList.valueOf(0xFF6366F1));
        title.setCompoundDrawablePadding(dp(8));
        top.addView(title, new LinearLayout.LayoutParams(0, -2, 1));
        boardDot = new TextView(this);
        boardDot.setText("● 探测中");
        boardDot.setTextSize(13);
        boardDot.setTextColor(PaTheme.LTXT2);
        boardDot.setBackground(PaTheme.pill(PaTheme.LWELL));
        boardDot.setPadding(dp(10), dp(4), dp(10), dp(4));
        top.addView(boardDot);
        TextView exit = btn("✕ 退出", PaTheme.LWELL, PaTheme.LTXT2);
        exit.setTextSize(14);                       // 38dp 固定高内 17sp 会裁下缘
        exit.setPadding(dp(2), 0, dp(2), 0);
        exit.setOnClickListener(new View.OnClickListener() { public void onClick(View v) { finish(); }});
        LinearLayout.LayoutParams ep = new LinearLayout.LayoutParams(dp(86), dp(38));
        ep.leftMargin = dp(8);
        top.addView(exit, ep);
        root.addView(top);

        status = new TextView(this);
        status.setTextSize(13);
        status.setTextColor(PaTheme.LTXT2);
        status.setTypeface(android.graphics.Typeface.MONOSPACE);
        status.setPadding(dp(14), 0, dp(14), dp(6));
        root.addView(status);

        // 控制行: 解锁 | 站立 | 挡位 (缩小上移, 不再和摇杆抢竖向空间)
        LinearLayout ctr = new LinearLayout(this);
        ctr.setOrientation(LinearLayout.HORIZONTAL);
        ctr.setGravity(Gravity.CENTER_VERTICAL);
        ctr.setPadding(dp(12), dp(8), dp(12), dp(6));
        armBtn = btn("解锁", PaTheme.LWELL, PaTheme.LTXT);
        armBtn.setOnClickListener(new View.OnClickListener() { public void onClick(View v) {
            lastUserActT = System.currentTimeMillis();
            armed = !armed;
            refreshBtns();
        }});
        ctr.addView(armBtn, new LinearLayout.LayoutParams(0, dp(44), 1f));
        batt = new BatteryView(this);
        LinearLayout.LayoutParams btp = new LinearLayout.LayoutParams(dp(86), dp(22));
        btp.leftMargin = dp(8);
        ctr.addView(batt, btp);
        walkBtn = btn("站立", PaTheme.LWELL, PaTheme.LTXT);
        walkBtn.setOnClickListener(new View.OnClickListener() { public void onClick(View v) {
            lastUserActT = System.currentTimeMillis();
            walk = !walk;
            refreshBtns();
        }});
        LinearLayout.LayoutParams wp = new LinearLayout.LayoutParams(0, dp(44), 1f);
        wp.leftMargin = dp(8);
        ctr.addView(walkBtn, wp);

        // 挡位 = 三段分段器(白卡容器+描边, 选中实心蓝白字)
        LinearLayout gears = new LinearLayout(this);
        gears.setOrientation(LinearLayout.HORIZONTAL);
        gears.setPadding(dp(4), dp(4), dp(4), dp(4));
        GradientDrawable gearsBg = PaTheme.fill(PaTheme.LCARD, 20);
        gearsBg.setStroke(dp(1), PaTheme.LDIV);
        gears.setBackground(gearsBg);
        for (int i = 0; i < 3; i++) {
            final int g = i + 1;
            gearBtns[i] = btn(String.valueOf(g), 0x00000000, PaTheme.LTXT2);
            gearBtns[i].setTextSize(15);
            gearBtns[i].setPadding(dp(2), dp(2), dp(2), dp(2));   // 44dp 行高内防下缘裁切
            gearBtns[i].setOnClickListener(new View.OnClickListener() { public void onClick(View v) {
                lastUserActT = System.currentTimeMillis();
                gear = g;
                refreshBtns();
            }});
            LinearLayout.LayoutParams gp = new LinearLayout.LayoutParams(0, -1, 1);
            if (i > 0) gp.leftMargin = dp(4);
            gears.addView(gearBtns[i], gp);
        }
        LinearLayout.LayoutParams gp2 = new LinearLayout.LayoutParams(0, dp(44), 1.5f);
        gp2.leftMargin = dp(8);
        ctr.addView(gears, gp2);
        root.addView(ctr);

        // 双摇杆(与实体遥控一致): 左=前后/转向 右=侧移
        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.HORIZONTAL);
        body.setPadding(dp(10), dp(2), dp(10), dp(4));
        joyL = new JoyView(); joyL.id = 0;
        joyR = new JoyView(); joyR.id = 1;
        LinearLayout.LayoutParams lpL = new LinearLayout.LayoutParams(0, -1, 1f);
        lpL.rightMargin = dp(8);
        body.addView(captioned(joyL, "左杆 · 前后 / 左右移动"), lpL);
        // 急停圆钮居中放两杆之间: 腾出原整行高度给摇杆(红实心, 安全可见性优先)
        TextView estop = btn("急停", PaTheme.LRED_BG, 0xFFFFFFFF);
        estop.setTextSize(16);
        estop.setPadding(dp(2), dp(2), dp(2), dp(2));
        ((GradientDrawable) estop.getBackground()).setCornerRadius(dp(40));
        estop.setOnClickListener(new View.OnClickListener() { public void onClick(View v) {
            armed = false;
            walk = false;
            joyX = 0; joyY = 0; joy1X = 0;
            if (joyL != null) { joyL.nx = 0; joyL.ny = 0; joyL.invalidate(); }
            if (joyR != null) { joyR.nx = 0; joyR.ny = 0; joyR.invalidate(); }
            refreshBtns();
        }});
        LinearLayout mid = new LinearLayout(this);
        mid.setOrientation(LinearLayout.VERTICAL);
        mid.setGravity(Gravity.CENTER);
        mid.addView(estop, new LinearLayout.LayoutParams(dp(80), dp(80)));
        LinearLayout.LayoutParams midp = new LinearLayout.LayoutParams(-2, -1);
        midp.leftMargin = dp(4);
        midp.rightMargin = dp(4);
        body.addView(mid, midp);
        body.addView(captioned(joyR, "右杆 · 左右转向"), new LinearLayout.LayoutParams(0, -1, 1f));
        root.addView(body, new LinearLayout.LayoutParams(-1, 0, 1f));

        TextView tip = new TextView(this);
        tip.setText("通道语义与实体遥控一致: 解锁→站立→摇杆  ·  挡位=步态(经济/自由/奔跑)\n"
                + "退出或切后台即停发, 约0.55s 站定; 30s 无信号上锁保持姿态(不趴下)");
        tip.setTextSize(12);
        tip.setTextColor(PaTheme.LTXT2);
        tip.setGravity(Gravity.CENTER);
        tip.setPadding(dp(10), dp(6), dp(10), dp(12));
        root.addView(tip);

        setContentView(root);
        refreshBtns();
    }

    void refreshBtns() {
        armBtn.setText(armed ? "上锁" : "解锁");
        ((GradientDrawable) armBtn.getBackground()).setColor(armed ? PaTheme.LBLUE : PaTheme.LWELL);
        ((GradientDrawable) armBtn.getBackground()).setStroke(dp(2), armed ? 0xFF2563EB : 0xFF9CA3AF);
        armBtn.setTextColor(armed ? 0xFFFFFFFF : PaTheme.LTXT);
        walkBtn.setText(walk ? "趴下" : "站立");
        ((GradientDrawable) walkBtn.getBackground()).setColor(walk ? PaTheme.LBLUE : PaTheme.LWELL);
        ((GradientDrawable) walkBtn.getBackground()).setStroke(dp(2), armed ? 0xFF2563EB : 0xFF9CA3AF);
        walkBtn.setTextColor(walk ? 0xFFFFFFFF : PaTheme.LTXT);
        for (int i = 0; i < 3; i++) {
            boolean on = (gear == i + 1);
            ((GradientDrawable) gearBtns[i].getBackground()).setColor(on ? PaTheme.LBLUE : 0x00000000);
            gearBtns[i].setTextColor(on ? 0xFFFFFFFF : PaTheme.LTXT2);
        }
    }

    // ============ 摇杆 ============

    class JoyView extends View {
        float nx = 0, ny = 0;
        int id = 0;               // 0=左杆(前后/转向) 1=右杆(侧移)
        Paint pRing = new Paint(Paint.ANTI_ALIAS_FLAG);
        Paint pKnob = new Paint(Paint.ANTI_ALIAS_FLAG);
        Paint pMark = new Paint(Paint.ANTI_ALIAS_FLAG);

        JoyView() {
            super(ManualCtrlActivity.this);
            pRing.setColor(0xFFFFFFFF);
            pRing.setStyle(Paint.Style.FILL);
            pKnob.setColor(PaTheme.LBLUE);
            pMark.setColor(0xFFD1D5DB);
            pMark.setStrokeWidth(dp(2));
        }

        @Override
        protected void onDraw(Canvas c) {
            float cx = getWidth() / 2f, cy = getHeight() / 2f;
            float r = Math.min(cx, cy) - dp(10);
            c.drawCircle(cx, cy, r, pRing);
            c.drawLine(cx - r * 0.85f, cy, cx + r * 0.85f, cy, pMark);
            c.drawLine(cx, cy - r * 0.85f, cx, cy + r * 0.85f, pMark);
            c.drawCircle(cx + nx * r * 0.70f, cy - ny * r * 0.70f, r * 0.32f, pKnob);
        }

        @Override
        public boolean onTouchEvent(MotionEvent e) {
            float cx = getWidth() / 2f, cy = getHeight() / 2f;
            float r = Math.min(cx, cy) - dp(10);
            int act = e.getAction();
            if (act == MotionEvent.ACTION_DOWN || act == MotionEvent.ACTION_MOVE) {
                float dx = (e.getX() - cx) / r, dy = (cy - e.getY()) / r;
                double m = Math.sqrt(dx * dx + dy * dy);
                if (m > 1) { dx /= m; dy /= m; }
                nx = dx; ny = dy;
            } else {
                nx = 0; ny = 0;
            }
            if (id == 0) { joyX = nx; joyY = ny; } else { joy1X = nx; }
            invalidate();
            return true;
        }
    }
}
