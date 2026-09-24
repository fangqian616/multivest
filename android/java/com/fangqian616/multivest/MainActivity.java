package com.fangqian616.multivest;

import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

/**
 * 手机端主界面：连接电脑上运行的后端，并用 WebView 呈现完整界面。
 *
 * 设计取舍
 * --------
 * 手机上不跑 Python —— 后端（FastAPI + 内置 8 年行情数据）在电脑上，
 * 这个 APK 只负责"把手机变成同一后端的第二个屏幕"。好处是手机与电脑
 * 永远看到同一份数据；代价是必须与电脑处于同一局域网，且电脑上的服务在运行。
 *
 * 为什么全是具名类、没有一个匿名内部类
 * ------------------------------------
 * 原本用匿名内部类写监听器，结果 d8（build-tools 34.0.0 自带的 R8 8.2）
 * 在读 `MainActivity$1.class` 时抛 NullPointerException 并以 "internal error"
 * 结束 —— 同一个类文件 javap 反汇编完全正常，加 `-g` 也不解决。
 * 改成具名嵌套类后构建即通过。
 * 因此这里刻意保留具名写法：**不要让"看着更简洁"的匿名类把这个构建链再弄坏一次**。
 *
 * 构建链：aapt2 + javac + d8 + zipalign + apksigner，见 tools/make_apk.py。
 * 不用 Gradle，也就不需要下载上百 MB 的 AGP 与 AndroidX 依赖。
 */
public class MainActivity extends Activity
        implements View.OnClickListener, TextView.OnEditorActionListener {

    private static final String PREFS = "multivest";
    private static final String KEY_URL = "server_url";
    private static final String DEFAULT_URL = "http://192.168.1.5:8760";

    private SharedPreferences prefs;
    private LinearLayout setupPanel;
    private LinearLayout browserPanel;
    private EditText urlInput;
    private TextView statusText;
    private WebView webView;
    private ProgressBar progress;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        buildUi();

        String saved = prefs.getString(KEY_URL, "");
        if (saved == null || saved.length() == 0) {
            showSetup();
        } else {
            showBrowser(saved);
        }
    }

    // ── 界面 ──────────────────────────────────────────────────────────────

    private int dp(float v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.parseColor("#F7F7F5"));

        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        progress.setVisibility(View.GONE);
        root.addView(progress, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(3)));

        setupPanel = buildSetupPanel();
        root.addView(setupPanel, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        browserPanel = new LinearLayout(this);
        browserPanel.setOrientation(LinearLayout.VERTICAL);
        webView = new WebView(this);
        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);          // 前端用 localStorage 存偏好
        ws.setLoadWithOverviewMode(true);
        ws.setUseWideViewPort(true);
        ws.setBuiltInZoomControls(false);
        ws.setCacheMode(WebSettings.LOAD_DEFAULT);
        webView.setWebViewClient(new AppWebViewClient(this));
        webView.setWebChromeClient(new AppChromeClient(this));
        browserPanel.addView(webView, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        browserPanel.setVisibility(View.GONE);
        root.addView(browserPanel, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        setContentView(root);
    }

    private LinearLayout buildSetupPanel() {
        LinearLayout outer = new LinearLayout(this);
        outer.setOrientation(LinearLayout.VERTICAL);

        ScrollView scroll = new ScrollView(this);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(24), dp(40), dp(24), dp(24));

        TextView title = new TextView(this);
        title.setText("连接电脑上的服务");
        title.setTextSize(24);
        title.setTextColor(Color.parseColor("#B01B2E"));
        title.setPadding(0, 0, 0, dp(14));
        box.addView(title);

        TextView help = new TextView(this);
        help.setTextSize(14);
        help.setTextColor(Color.parseColor("#3A3A3A"));
        help.setLineSpacing(dp(4), 1.15f);
        help.setText(
            "本应用把手机变成电脑端服务的第二个屏幕 —— 手机上不运行任何计算，\n"
          + "所有行情与研判结果都来自电脑，因此两端看到的内容完全一致。\n\n"
          + "使用前提：\n"
          + "  1. 电脑上已打开「智能多维投资系统」，且服务正在运行\n"
          + "  2. 手机与电脑连接同一个 WiFi\n\n"
          + "获取地址：在电脑端点顶部「连接」标签，页面上会显示形如\n"
          + "http://192.168.x.x:8760 的地址，也可直接扫页面上的二维码。\n\n"
          + "本项目的量化预测部分仅参考，请务必谨慎用于投资决策。");
        box.addView(help);

        TextView label = new TextView(this);
        label.setText("\n服务地址");
        label.setTextSize(13);
        label.setTextColor(Color.parseColor("#8A8A8A"));
        box.addView(label);

        urlInput = new EditText(this);
        urlInput.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        urlInput.setHint(DEFAULT_URL);
        urlInput.setText(DEFAULT_URL);
        urlInput.setSingleLine(true);
        urlInput.setImeOptions(EditorInfo.IME_ACTION_GO);
        urlInput.setOnEditorActionListener(this);
        box.addView(urlInput);

        Button btn = new Button(this);
        btn.setText("连接");
        btn.setOnClickListener(this);
        LinearLayout.LayoutParams blp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        blp.topMargin = dp(16);
        box.addView(btn, blp);

        statusText = new TextView(this);
        statusText.setTextSize(13);
        statusText.setTextColor(Color.parseColor("#8A6A1F"));
        statusText.setPadding(0, dp(14), 0, 0);
        box.addView(statusText);

        scroll.addView(box);
        outer.addView(scroll, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        return outer;
    }

    // ── 监听器（具名类，见类注释里关于 d8 的说明）─────────────────────────

    @Override
    public void onClick(View v) {
        connect();
    }

    @Override
    public boolean onEditorAction(TextView v, int actionId, KeyEvent event) {
        if (actionId == EditorInfo.IME_ACTION_GO) {
            connect();
            return true;
        }
        return false;
    }

    /** WebView 客户端：加载完成即收起进度条。 */
    private static class AppWebViewClient extends WebViewClient {
        private final MainActivity host;

        AppWebViewClient(MainActivity host) {
            this.host = host;
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            host.progress.setVisibility(View.GONE);
        }
    }

    /** 进度回调：把 WebView 的加载进度接到顶部进度条上。 */
    private static class AppChromeClient extends WebChromeClient {
        private final MainActivity host;

        AppChromeClient(MainActivity host) {
            this.host = host;
        }

        @Override
        public void onProgressChanged(WebView view, int p) {
            host.progress.setVisibility(p < 100 ? View.VISIBLE : View.GONE);
            host.progress.setProgress(p);
        }
    }

    // ── 行为 ──────────────────────────────────────────────────────────────

    /** 补全用户可能省略的协议前缀 —— 手输地址时最常见的错误就是漏掉 http://。 */
    private String normalize(String raw) {
        String u = raw == null ? "" : raw.trim();
        if (u.length() == 0) {
            return "";
        }
        if (!u.startsWith("http://") && !u.startsWith("https://")) {
            u = "http://" + u;
        }
        while (u.endsWith("/")) {
            u = u.substring(0, u.length() - 1);
        }
        return u;
    }

    private void connect() {
        String url = normalize(urlInput.getText().toString());
        if (url.length() == 0) {
            Toast.makeText(this, "请填写电脑上显示的服务地址", Toast.LENGTH_SHORT).show();
            return;
        }
        prefs.edit().putString(KEY_URL, url).apply();
        showBrowser(url);
    }

    private void showSetup() {
        setupPanel.setVisibility(View.VISIBLE);
        browserPanel.setVisibility(View.GONE);
        String saved = prefs.getString(KEY_URL, "");
        if (saved != null && saved.length() > 0) {
            urlInput.setText(saved);
        }
        urlInput.requestFocus();
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_VISIBLE);
    }

    private void showBrowser(String url) {
        setupPanel.setVisibility(View.GONE);
        browserPanel.setVisibility(View.VISIBLE);
        progress.setVisibility(View.VISIBLE);
        webView.loadUrl(url);
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, 1, 0, "刷新");
        menu.add(0, 2, 1, "修改服务地址");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        if (item.getItemId() == 1) {
            String saved = prefs.getString(KEY_URL, "");
            if (saved != null && saved.length() > 0) {
                showBrowser(saved);
            } else {
                showSetup();
            }
            return true;
        }
        if (item.getItemId() == 2) {
            webView.stopLoading();
            showSetup();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        // 返回键优先在页面内后退，退无可退时回到地址设置页，最后才退出应用。
        if (keyCode == KeyEvent.KEYCODE_BACK
                && browserPanel.getVisibility() == View.VISIBLE) {
            if (webView.canGoBack()) {
                webView.goBack();
                return true;
            }
            showSetup();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }
}
