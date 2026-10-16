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
    volatile float joyX = 0f, joyY = 0f;   // 摇杆 -1..1 (Y 上=前进)
    volatile boolean sending = false;
    int seq = 0;

    TextView status, boardDot;
    TextView armBtn, walkBtn;
    TextView[] gearBtns = new TextView[3];
    Handler ui = new Handler();
    DatagramSocket sock;
    Thread sender, prober;
    JoyView joy;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
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
            while (sending) {
                boolean a, w; int g, s; float jx, jy;
                synchronized (ManualCtrlActivity.this) {
                    a = armed; w = walk; g = gear; jx = joyX; jy = joyY; s = ++seq;
                }
                StringBuilder js = new StringBuilder(140);
                js.append("{\"seq\":").append(s).append(",\"ch\":[");
                for (int i = 0; i < 16; i++) {
                    int v = 1002;
                    if (i == 0) v = 1002 + (int) (jx * 720);            // 转向: 右推值增(ch0 右+)
                    else if (i == 1) v = 1002 - (int) (jy * 720);       // 前后: 实体标定"左竖下+", 上推=值减!
                    else if (i == 4) v = a ? 1722 : 1002;               // A 解锁位
                    else if (i == 6) v = w ? 1722 : 282;                // C 上=站/走
                    else if (i == 9) v = (g == 1) ? 282 : (g == 2 ? 1002 : 1722);
                    js.append(i == 0 ? "" : ",").append(v);
                }
                js.append("]}");
                try {
                    byte[] ob = js.toString().getBytes("UTF-8");
                    if (sock != null) sock.send(new DatagramPacket(ob, ob.length,
                            InetAddress.getByName(PaService.DST_IP), NETRC_PORT));
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
                            InetAddress.getByName(PaService.DST_IP), PaService.CTRL_PORT));
                    byte[] rb = new byte[96];
                    DatagramPacket rp = new DatagramPacket(rb, rb.length);
                    s.receive(rp);
                    r = new String(rb, 0, rp.getLength(), "UTF-8");
                    // 第二问: 桥的真实 RC 状态
                    byte[] ob2 = "RC".getBytes();
                    s.send(new DatagramPacket(ob2, ob2.length,
                            InetAddress.getByName(PaService.DST_IP), PaService.CTRL_PORT));
                    DatagramPacket rp2 = new DatagramPacket(rb, rb.length);
                    try { s.receive(rp2); rc = new String(rb, 0, rp2.getLength(), "UTF-8"); } catch (Exception ig) {}
                } catch (Exception e) {
                } finally { try { if (s != null) s.close(); } catch (Exception ig) {} }
                final boolean ok = r.startsWith("OK");
                final String boardRc = rc;   // "RC 1 2 1" = 解锁/行走/挡1; 空=没回
                ui.post(new Runnable() { public void run() {
                    if (boardDot != null) {
                        boardDot.setText(ok ? ("● 板子在线" + fmtRc(boardRc)) : "● 板子无响应");
                        boardDot.setTextColor(ok ? 0xFF5AD191 : 0xFFFF6459);
                    }
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

    void tickStatus() {
        ui.postDelayed(new Runnable() { public void run() {
            if (status != null) {
                status.setText(String.format("seq %d · %s · 挡%d · 摇杆 %.2f/%.2f",
                        seq, armed ? "已解锁" : "上锁", gear, joyX, joyY));
            }
            if (sending) tickStatus();
        }}, 500);
    }

    // ============ UI ============

    int dp(int v) { return Math.round(v * getResources().getDisplayMetrics().density); }

    TextView btn(String text, int bg, int fg) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextSize(17);
        t.setTypeface(null, android.graphics.Typeface.BOLD);
        t.setTextColor(fg);
        t.setGravity(Gravity.CENTER);
        GradientDrawable d = new GradientDrawable();
        d.setColor(bg);
        d.setCornerRadius(dp(12));
        t.setBackground(d);
        t.setPadding(dp(8), dp(10), dp(8), dp(10));
        return t;
    }

    void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(0xFF0B1220);

        // 顶栏
        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.HORIZONTAL);
        top.setGravity(Gravity.CENTER_VERTICAL);
        top.setPadding(dp(12), dp(10), dp(12), dp(4));
        TextView title = new TextView(this);
        title.setText("🎮 手动控制");
        title.setTextSize(19);
        title.setTypeface(null, android.graphics.Typeface.BOLD);
        title.setTextColor(0xFFE2E8F0);
        top.addView(title, new LinearLayout.LayoutParams(0, -2, 1));
        boardDot = new TextView(this);
        boardDot.setText("● 探测中");
        boardDot.setTextSize(13);
        top.addView(boardDot);
        TextView exit = btn("✕ 退出", 0xFF263043, 0xFFE2E8F0);
        exit.setOnClickListener(new View.OnClickListener() { public void onClick(View v) { finish(); }});
        LinearLayout.LayoutParams ep = new LinearLayout.LayoutParams(dp(86), dp(38));
        ep.leftMargin = dp(8);
        top.addView(exit, ep);
        root.addView(top);

        status = new TextView(this);
        status.setTextSize(13);
        status.setTextColor(0xFF8B948F);
        status.setPadding(dp(14), 0, dp(14), dp(6));
        root.addView(status);

        // 主体: 左摇杆 右按键列
        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.HORIZONTAL);
        body.setPadding(dp(10), dp(4), dp(10), dp(10));

        joy = new JoyView();
        LinearLayout.LayoutParams jp = new LinearLayout.LayoutParams(0, -1, 1.25f);
        jp.rightMargin = dp(10);
        body.addView(joy, jp);

        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);

        armBtn = btn("🔓 解锁", 0xFF3A1F1C, 0xFFFFB4A8);
        armBtn.setOnClickListener(new View.OnClickListener() { public void onClick(View v) {
            armed = !armed;
            refreshBtns();
        }});
        LinearLayout.LayoutParams ap2 = new LinearLayout.LayoutParams(-1, 0, 1.1f);
        ap2.bottomMargin = dp(8);
        col.addView(armBtn, ap2);

        walkBtn = btn("⬆ 站立", 0xFF1E2A3F, 0xFF9BC1FF);
        walkBtn.setOnClickListener(new View.OnClickListener() { public void onClick(View v) {
            walk = !walk;
            refreshBtns();
        }});
        LinearLayout.LayoutParams wp = new LinearLayout.LayoutParams(-1, 0, 1.1f);
        wp.bottomMargin = dp(8);
        col.addView(walkBtn, wp);

        LinearLayout gears = new LinearLayout(this);
        gears.setOrientation(LinearLayout.HORIZONTAL);
        for (int i = 0; i < 3; i++) {
            final int g = i + 1;
            gearBtns[i] = btn("挡" + g, 0xFF1A201D, 0xFF8B948F);
            gearBtns[i].setOnClickListener(new View.OnClickListener() { public void onClick(View v) {
                gear = g;
                refreshBtns();
            }});
            LinearLayout.LayoutParams gp = new LinearLayout.LayoutParams(0, -1, 1);
            if (i > 0) gp.leftMargin = dp(6);
            gears.addView(gearBtns[i], gp);
        }
        LinearLayout.LayoutParams gp2 = new LinearLayout.LayoutParams(-1, 0, 0.62f);
        gp2.bottomMargin = dp(8);
        col.addView(gears, gp2);

        TextView estop = btn("⛔ 急停·趴下", 0xFF7A1E16, 0xFFFFD5CE);
        estop.setOnClickListener(new View.OnClickListener() { public void onClick(View v) {
            armed = false;
            walk = false;
            joyX = 0; joyY = 0;
            if (joy != null) { joy.nx = 0; joy.ny = 0; joy.invalidate(); }
            refreshBtns();
        }});
        LinearLayout.LayoutParams esp = new LinearLayout.LayoutParams(-1, 0, 0.8f);
        col.addView(estop, esp);

        body.addView(col, new LinearLayout.LayoutParams(0, -1, 1f));
        root.addView(body, new LinearLayout.LayoutParams(-1, 0, 1f));

        TextView tip = new TextView(this);
        tip.setText("通道语义与实体遥控一致: 解锁→站立→摇杆  ·  挡位=步态(经济/自由/奔跑)\n"
                + "退出或切后台即停发, 约0.55s 站定; 30s 无信号上锁保持姿态(不趴下)");
        tip.setTextSize(12);
        tip.setTextColor(0xFF6B7280);
        tip.setGravity(Gravity.CENTER);
        tip.setPadding(dp(10), dp(6), dp(10), dp(12));
        root.addView(tip);

        setContentView(root);
        refreshBtns();
    }

    void refreshBtns() {
        armBtn.setText(armed ? "🔒 上锁" : "🔓 解锁");
        ((GradientDrawable) armBtn.getBackground()).setColor(armed ? 0xFF123524 : 0xFF3A1F1C);
        armBtn.setTextColor(armed ? 0xFF9BEFC5 : 0xFFFFB4A8);
        walkBtn.setText(walk ? "⬇ 趴下" : "⬆ 站立");
        ((GradientDrawable) walkBtn.getBackground()).setColor(walk ? 0xFF233046 : 0xFF1E2A3F);
        walkBtn.setTextColor(walk ? 0xFFBAD6FF : 0xFF9BC1FF);
        for (int i = 0; i < 3; i++) {
            boolean on = (gear == i + 1);
            ((GradientDrawable) gearBtns[i].getBackground()).setColor(on ? 0xFF2F6B4D : 0xFF1A201D);
            gearBtns[i].setTextColor(on ? 0xFF9BEFC5 : 0xFF8B948F);
        }
    }

    // ============ 摇杆 ============

    class JoyView extends View {
        float nx = 0, ny = 0;
        Paint pRing = new Paint(Paint.ANTI_ALIAS_FLAG);
        Paint pKnob = new Paint(Paint.ANTI_ALIAS_FLAG);
        Paint pMark = new Paint(Paint.ANTI_ALIAS_FLAG);

        JoyView() {
            super(ManualCtrlActivity.this);
            pRing.setColor(0xFF141B2B);
            pRing.setStyle(Paint.Style.FILL);
            pKnob.setColor(0xFF2563EB);
            pMark.setColor(0xFF33415E);
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
            joyX = nx; joyY = ny;
            invalidate();
            return true;
        }
    }
}
