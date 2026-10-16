package com.wjl.pasend;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.net.Uri;

/** 透明中转: 由悬浮窗拉起文件选择器, 选完把 Uri 交给 PaService 处理后自毁 */
public class ImportActivity extends Activity {

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        Intent i = new Intent(Intent.ACTION_GET_CONTENT);
        i.setType("audio/*");
        i.addCategory(Intent.CATEGORY_OPENABLE);
        startActivityForResult(Intent.createChooser(i, "选择音频文件"), 11);
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        if (req == 11 && res == RESULT_OK && data != null && data.getData() != null) {
            Uri uri = data.getData();
            finish();                       // 先退, 解码在服务里跑
            PaService.handleImport(this, uri);
            return;
        }
        finish();
    }
}
