package com.wjl.pasend;

import android.graphics.Canvas;
import android.graphics.ColorFilter;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.content.res.Resources;

/**
 * FLstyle 工具类深色包 tokens (v37 重构, 全 app 唯一颜色来源)
 * 表面阶梯(嵌套只朝亮一档): 画布#131315 → 框架#1B1B1E → 卡#252628 → 次表面#323235 → 交互井#404042
 * 强调蓝 #2F83FF 只做合法载体(选中实心/开关ON/图标井/前景图标/计数底/进度弧);
 * 状态色只上文字与小标记, 不上整块背景; 一屏唯一主动作 = 蓝实心.
 * 有意偏离一处: 急停按钮 ESTOP 红实心(安全可见性优先, 2026-10-30 设计评审记录).
 */
final class PaTheme {
    static final int CANVAS = 0xFF131315;
    static final int FRAME  = 0xFF1B1B1E;   // 对话框/WebView 托底
    static final int PANEL  = 0xF6252628;   // 悬浮窗卡面(96% 不透明)
    static final int CARD   = 0xFF252628;
    static final int SUB    = 0xFF323235;
    static final int WELL   = 0xFF404042;
    static final int DIVIDER= 0xFF424346;
    static final int TXT    = 0xFFF5F6FF;
    static final int TXT2   = 0xFFC1C6D7;
    static final int BLUE   = 0xFF2F83FF;
    static final int BLUE15 = 0x262F83FF;   // primary 15% 底(配 primary 字)
    static final int GREEN  = 0xFF94F990;
    static final int GREENBG= 0xFF005313;
    static final int RED    = 0xFFFF6369;
    static final int WARN   = 0xFFFBBF24;
    static final int ESTOP  = 0xFFE5484D;

    /* v38 浅色壳(与网页浅色主题同源): 主界面壳/底部分段组用; 悬浮台/对话框仍用上方深色 tokens */
    static final int LBG    = 0xFFF5F6FA;   // 壳背景 = 网页底色, 融为一体无"黑边"
    static final int LWELL  = 0xFFF3F4F6;   // 分段容器底
    static final int LSEG   = 0xFFE5E7EB;   // 分段段底(常灰, v39: 用户定去掉蓝色选中态)
    static final int LBORDER= 0xFFE5E7EB;   // 分段容器描边
    static final int LTXT   = 0xFF1F2937;   // 未选中段文字/图标
    static final int LBLUE  = 0xFF3B82F6;   // 强调蓝(网页同源; v39 起分段不再用)
    static final int LBROWN  = 0xFFB45309;   // 琥珀(浅底上的警示文字)
    /* v41 浅色 tokens 第二批: 悬浮台/录音库/动作/对话框/手动页(网页同源) */
    static final int LCARD   = 0xF6FFFFFF;   // 卡面(96%白, 对应旧 PANEL/CARD)
    static final int LDIV    = 0xFFE5E7EB;   // 分隔线
    static final int LTXT2   = 0xFF6B7280;   // 次文字
    static final int LGREEN  = 0xFF059669;   // 绿(浅底上的文字/小标记)
    static final int LRED    = 0xFFDC2626;   // 红(浅底上的文字)
    static final int LRED_BG = 0xFFEF4444;   // 红/急停实心(=网页 danger)
    static final int LGREENBG= 0xFFD1FAE5;   // 在线徽章浅绿底

    /** 全局 dp 换算(无 Context 依赖, density 由系统资源提供) */
    static int dp(float v) {
        return Math.round(Resources.getSystem().getDisplayMetrics().density * v);
    }

    /** 实心圆角面(radiusDp=999 即胶囊) */
    static GradientDrawable fill(int color, float radiusDp) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(color);
        g.setCornerRadius(dp(radiusDp));
        return g;
    }

    /** 胶囊按钮面 */
    static GradientDrawable pill(int color) {
        return fill(color, 999);
    }

    /**
     * 花形 scallop 悬浮窗大钮(FLstyle B6: 瓣外鼓 10~12 瓣; 本 app 用 12 瓣)
     * 用于喊话气泡: 直径 100dp, 配白 glyph
     */
    static class Scallop extends Drawable {
        private final Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);

        Scallop(int color) { p.setColor(color); }

        @Override public void draw(Canvas c) {
            Rect b = getBounds();
            if (b.isEmpty()) return;
            float cx = b.centerX(), cy = b.centerY();
            // 瓣尖(谷)=0.86R, 瓣鼓(峰)≈R: 花形恰好贴满 bounds 不被裁
            float rTip = Math.min(b.width(), b.height()) / 2f * 0.86f;
            float rBulge = rTip * 1.16f;
            Path path = new Path();
            int n = 12;
            double step = 2 * Math.PI / n;
            for (int i = 0; i <= n; i++) {
                double a0 = i * step;
                double am = a0 + step / 2;
                double a1 = (i + 1) * step;
                float x0 = cx + (float) (rTip * Math.cos(a0));
                float y0 = cy + (float) (rTip * Math.sin(a0));
                float xm = cx + (float) (rBulge * Math.cos(am));
                float ym = cy + (float) (rBulge * Math.sin(am));
                float x1 = cx + (float) (rTip * Math.cos(a1));
                float y1 = cy + (float) (rTip * Math.sin(a1));
                if (i == 0) path.moveTo(x0, y0);
                path.quadTo(xm, ym, x1, y1);
            }
            path.close();
            c.drawPath(path, p);
        }

        @Override public void setAlpha(int alpha) { p.setAlpha(alpha); }
        @Override public void setColorFilter(ColorFilter cf) { p.setColorFilter(cf); }
        @Override public int getOpacity() { return PixelFormat.TRANSLUCENT; }
    }
}
