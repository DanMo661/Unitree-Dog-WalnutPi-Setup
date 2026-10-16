package com.wjl.pasend;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;

/** 把更新 APK 以 content:// 暴露给系统安装器(免 FileProvider 依赖) */
public class PaFiles extends ContentProvider {

    static final String AUTH = "com.wjl.pasend.files";

    static Uri apkUri() {
        return Uri.parse("content://" + AUTH + "/update.apk");
    }

    private File fileFor(Uri uri) {
        String p = uri.getPath();
        String name = (p == null || !p.startsWith("/")) ? "update.apk"
                : p.substring(1);
        if (name.contains("/") || name.contains("..")) name = "update.apk";
        return new File(getContext().getExternalFilesDir("update"), name);
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode)
            throws FileNotFoundException {
        File f = fileFor(uri);
        if (!f.exists()) throw new FileNotFoundException(uri.toString());
        return ParcelFileDescriptor.open(f,
                ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public Cursor query(Uri uri, String[] proj, String sel, String[] args, String sort) {
        File f = fileFor(uri);
        if (proj == null)
            proj = new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE};
        MatrixCursor c = new MatrixCursor(proj);
        android.database.MatrixCursor.RowBuilder b = c.newRow();
        for (String col : proj) {
            if (OpenableColumns.DISPLAY_NAME.equals(col)) b.add(f.getName());
            else if (OpenableColumns.SIZE.equals(col)) b.add(f.length());
            else b.add(null);
        }
        return c;
    }

    @Override
    public String getType(Uri uri) {
        return "application/vnd.android.package-archive";
    }

    @Override
    public boolean onCreate() { return true; }

    @Override
    public Uri insert(Uri uri, ContentValues values) { return null; }

    @Override
    public int delete(Uri uri, String sel, String[] args) { return 0; }

    @Override
    public int update(Uri uri, ContentValues v, String sel, String[] args) { return 0; }
}
