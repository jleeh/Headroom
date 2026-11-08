// Headroom launcher: runs elevated (manifest), starts the Python backend (headroom_server.pyw on :7871),
// shows the UI in a borderless WebView2 window with Headroom's own look, and stops the
// backend when the window closes. The page draws its own title bar and posts "drag", "size:<edge>",
// "min", "max", "close" to this host.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

class Headroom
{
    public const string Url = "http://127.0.0.1:7871/";
    // per-launch secret shared only with the backend we start (environment) and added by the window
    // to its own requests, so web pages and non-admin programs can't drive the elevated API
    public static string Token = Environment.GetEnvironmentVariable("HEADROOM_TOKEN") ?? NewToken();
    static string NewToken()
    {
        var b = new byte[32];
        using (var r = RandomNumberGenerator.Create()) r.GetBytes(b);
        return BitConverter.ToString(b).Replace("-", "").ToLowerInvariant();
    }
    public static string Dir = AppDomain.CurrentDomain.BaseDirectory;
    public static string Data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Headroom");

    [DllImport("user32.dll")] static extern IntPtr FindWindow(string cls, string title);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);

    [STAThread]
    static void Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--window-only")
        {   // test hook: just the window against an already-running backend
            Application.EnableVisualStyles(); Application.Run(new MainWindow(args.Length > 1 ? args[1] : "")); return;
        }
        bool first;
        using (var mutex = new Mutex(true, "Headroom.Launcher", out first))
        {
            if (!first)
            {
                var h = FindWindow(null, "Headroom");
                if (h != IntPtr.Zero) { ShowWindow(h, 9); SetForegroundWindow(h); }
                return;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            var splash = new Splash();
            splash.Show(); Application.DoEvents();

            Process py = null;
            int old = PingPid();
            if (old > 0)
            {   // a backend from an earlier session that we can't authenticate to: stop it and start our own
                try { var op = Process.GetProcessById(old); if (op.ProcessName.StartsWith("python", StringComparison.OrdinalIgnoreCase)) { op.Kill(); op.WaitForExit(5000); } } catch { }
            }
            if (PingPid() > 0)
            {
                splash.Close();
                MessageBox.Show("Another program is using Headroom's port 7871. Close it and start Headroom again.", "Headroom", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            {
                var pyw = FindPythonw();
                if (pyw == null)
                {
                    splash.Close();
                    MessageBox.Show("Headroom's Python runtime is missing. Reinstall Headroom (or install Python 3.12 for a source checkout).", "Headroom", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return;
                }
                var psi = new ProcessStartInfo(pyw, "\"" + Path.Combine(Dir, "headroom_server.pyw") + "\"")
                    { WorkingDirectory = Dir, UseShellExecute = false, CreateNoWindow = true };
                psi.EnvironmentVariables["HEADROOM_TOKEN"] = Token;
                py = Process.Start(psi);
                var t0 = DateTime.Now;
                while (PingPid() != py.Id)
                {
                    Application.DoEvents(); Thread.Sleep(200);
                    if (py.HasExited || (DateTime.Now - t0).TotalSeconds > 45)
                    {
                        splash.Close();
                        MessageBox.Show("The Headroom backend didn't start.\nSee %APPDATA%\\Headroom\\server_crash.log",
                            "Headroom", MessageBoxButtons.OK, MessageBoxIcon.Error);
                        return;
                    }
                }
            }

            string wv2 = null;
            try { wv2 = CoreWebView2Environment.GetAvailableBrowserVersionString(); } catch { }
            if (wv2 == null)
            {
                splash.Close();
                MessageBox.Show("Microsoft Edge WebView2 Runtime is missing.\n\nInstall it from https://developer.microsoft.com/microsoft-edge/webview2/ (Evergreen Runtime), then start Headroom again.", "Headroom", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            else
            {
                var win = new MainWindow("");
                win.Shown += (s, e) => splash.Close();
                Application.Run(win);
            }
            try { using (var wc = new WebClient()) { wc.Headers["X-Headroom-Token"] = Token; wc.Headers["Content-Type"] = "application/json"; wc.UploadString(Url + "api/shutdown", "{}"); } } catch { }
            if (py != null) { try { if (!py.WaitForExit(20000)) py.Kill(); } catch { } }
        }
    }

    static string FindPythonw()
    {
        var bundled = Path.Combine(Dir, @"runtime\pythonw.exe");   // installer build ships its own Python
        if (File.Exists(bundled)) return bundled;
        var la = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        foreach (var v in new[] { "Python312", "Python313", "Python314" })
        {
            var p = Path.Combine(la, @"Programs\Python\" + v + @"\pythonw.exe");
            if (File.Exists(p)) return p;
        }
        return null;
    }

    // pid of whatever answers on our port (0 = nothing)
    public static int PingPid()
    {
        try
        {
            var req = (HttpWebRequest)WebRequest.Create(Url + "api/ping"); req.Timeout = 1500;
            using (var r = req.GetResponse())
            using (var sr = new StreamReader(r.GetResponseStream()))
            {
                var m = System.Text.RegularExpressions.Regex.Match(sr.ReadToEnd(), "\"pid\"\\s*:\\s*(\\d+)");
                return m.Success ? int.Parse(m.Groups[1].Value) : -1;
            }
        }
        catch (WebException e) { return e.Response != null ? -1 : 0; }
        catch { return 0; }
    }
}

class MainWindow : Form
{
    [DllImport("user32.dll")] static extern bool ReleaseCapture();
    [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr h, int msg, IntPtr w, IntPtr l);
    [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr h, int attr, ref int val, int size);

    WebView2 web;
    string stateFile = Path.Combine(Headroom.Data, "window.txt");
    string hash;

    public MainWindow(string hash)
    {
        this.hash = hash;
        Text = "Headroom";
        FormBorderStyle = FormBorderStyle.None;
        BackColor = Color.FromArgb(14, 18, 34);
        MinimumSize = new Size(1180, 720);
        StartPosition = FormStartPosition.Manual;
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        RestoreBounds_();

        web = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = BackColor };
        Controls.Add(web);
        Load += async (s, e) =>
        {
            Directory.CreateDirectory(Headroom.Data);
            var env = await CoreWebView2Environment.CreateAsync(null, Path.Combine(Headroom.Data, "WebView2"));
            await web.EnsureCoreWebView2Async(env);
            var st = web.CoreWebView2.Settings;
            st.IsStatusBarEnabled = false; st.IsZoomControlEnabled = false; st.AreDefaultContextMenusEnabled = false;
            st.AreDevToolsEnabled = false; st.IsGeneralAutofillEnabled = false; st.IsPasswordAutosaveEnabled = false;
            // the token goes only on requests to our own backend
            web.CoreWebView2.AddWebResourceRequestedFilter(Headroom.Url + "*", CoreWebView2WebResourceContext.All);
            web.CoreWebView2.WebResourceRequested += (o, r) =>
            {
                if (r.Request.Uri.StartsWith(Headroom.Url, StringComparison.OrdinalIgnoreCase))
                    r.Request.Headers.SetHeader("X-Headroom-Token", Headroom.Token);
            };
            // the window only ever shows Headroom's own page; links go to the normal browser
            web.CoreWebView2.NavigationStarting += (o, n) =>
            {
                if (!n.Uri.StartsWith(Headroom.Url, StringComparison.OrdinalIgnoreCase)) { n.Cancel = true; OpenExternal(n.Uri); }
            };
            web.CoreWebView2.FrameNavigationStarting += (o, n) => { if (!n.Uri.StartsWith(Headroom.Url, StringComparison.OrdinalIgnoreCase)) n.Cancel = true; };
            web.CoreWebView2.WebMessageReceived += (o, m) =>
            {
                if (m.Source.StartsWith(Headroom.Url, StringComparison.OrdinalIgnoreCase)) OnMessage(m.TryGetWebMessageAsString());
            };
            web.CoreWebView2.NavigationCompleted += (o, n) => Notify();
            web.CoreWebView2.NewWindowRequested += (o, n) => { n.Handled = true; OpenExternal(n.Uri); };
            web.Source = new Uri(Headroom.Url + hash);
        };
        Resize += (s, e) => Notify();
        FormClosing += (s, e) => SaveBounds();
    }

    static void OpenExternal(string uri)
    {   // we run elevated: open links in the user's normal (non-admin) browser via Explorer.
        // Only plain web links: never file paths or other protocol handlers.
        Uri u;
        if (!Uri.TryCreate(uri, UriKind.Absolute, out u) || (u.Scheme != Uri.UriSchemeHttps && u.Scheme != Uri.UriSchemeHttp)) return;
        if (u.IsLoopback) return;
        try { Process.Start(new ProcessStartInfo("explorer.exe", "\"" + u.AbsoluteUri.Replace("\"", "%22") + "\"") { UseShellExecute = false }); } catch { }
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        int round = 2; DwmSetWindowAttribute(Handle, 33, ref round, 4);
        int none = unchecked((int)0xFFFFFFFE); DwmSetWindowAttribute(Handle, 34, ref none, 4);
        int dark = 1; DwmSetWindowAttribute(Handle, 20, ref dark, 4);
    }

    protected override CreateParams CreateParams
    {
        get { var cp = base.CreateParams; cp.Style |= 0x00020000 | 0x00010000; return cp; }
    }

    void OnMessage(string m)
    {
        if (m == null) return;
        if (m == "drag" && WindowState != FormWindowState.Maximized) { ReleaseCapture(); SendMessage(Handle, 0xA1, (IntPtr)2, IntPtr.Zero); }
        else if (m == "drag")
        {
            var c = Cursor.Position; float fx = (float)(c.X - Left) / Width;
            WindowState = FormWindowState.Normal; Left = c.X - (int)(Width * fx); Top = c.Y - 20;
            ReleaseCapture(); SendMessage(Handle, 0xA1, (IntPtr)2, IntPtr.Zero);
        }
        else if (m.StartsWith("size:") && WindowState == FormWindowState.Normal)
        {
            int ht = 0;
            switch (m.Substring(5)) { case "w": ht = 10; break; case "e": ht = 11; break; case "n": ht = 12; break; case "nw": ht = 13; break;
                case "ne": ht = 14; break; case "s": ht = 15; break; case "sw": ht = 16; break; case "se": ht = 17; break; }
            if (ht != 0) { ReleaseCapture(); SendMessage(Handle, 0xA1, (IntPtr)ht, IntPtr.Zero); }
        }
        else if (m == "min") WindowState = FormWindowState.Minimized;
        else if (m == "max") ToggleMax();
        else if (m == "close") Close();
    }

    void ToggleMax()
    {
        if (WindowState == FormWindowState.Maximized) WindowState = FormWindowState.Normal;
        else { MaximizedBounds = Screen.FromControl(this).WorkingArea; WindowState = FormWindowState.Maximized; }
    }

    void Notify()
    {
        try { if (web != null && web.CoreWebView2 != null) web.CoreWebView2.PostWebMessageAsString(WindowState == FormWindowState.Maximized ? "maxed" : "normal"); } catch { }
    }

    void RestoreBounds_()
    {
        var wa = Screen.PrimaryScreen.WorkingArea;
        int w = Math.Min(1420, wa.Width - 40), h = Math.Min(900, wa.Height - 40);
        Bounds = new Rectangle(wa.X + (wa.Width - w) / 2, wa.Y + (wa.Height - h) / 2, w, h);
        try
        {
            var p = File.ReadAllText(stateFile).Split(',');
            var r = new Rectangle(int.Parse(p[0]), int.Parse(p[1]), int.Parse(p[2]), int.Parse(p[3]));
            foreach (var s in Screen.AllScreens) if (s.WorkingArea.IntersectsWith(r)) { Bounds = r; break; }
            if (p.Length > 4 && p[4] == "max") Shown += (o, e) => ToggleMax();
        }
        catch { }
    }

    void SaveBounds()
    {
        try
        {
            Directory.CreateDirectory(Headroom.Data);
            var r = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
            File.WriteAllText(stateFile, r.X + "," + r.Y + "," + r.Width + "," + r.Height + (WindowState == FormWindowState.Maximized ? ",max" : ""));
        }
        catch { }
    }
}

class Splash : Form
{
    public Splash()
    {
        FormBorderStyle = FormBorderStyle.None; StartPosition = FormStartPosition.Manual;
        Size = new Size(400, 150); BackColor = Color.FromArgb(7, 9, 18); ShowInTaskbar = true; TopMost = true; DoubleBuffered = true;
        Text = "Headroom starting";
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        var scr = Screen.FromPoint(Cursor.Position).WorkingArea;
        Location = new Point(scr.X + (scr.Width - Width) / 2, scr.Y + (scr.Height - Height) / 2);
        var path = Round(new Rectangle(0, 0, Width, Height), 22);
        Region = new Region(path);
        var t = new System.Windows.Forms.Timer { Interval = 40 }; int tick = 0; t.Tick += (s, e) => { tick++; Invalidate(); }; t.Start();
        Paint += (s, e) =>
        {
            var g = e.Graphics; g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.AntiAliasGridFit;
            var r = new Rectangle(0, 0, Width - 1, Height - 1);
            using (var bg = new System.Drawing.Drawing2D.LinearGradientBrush(r, Color.FromArgb(14, 18, 34), Color.FromArgb(27, 23, 48), 35f)) g.FillPath(bg, path);
            Glow(g, new Rectangle(-120, -110, 300, 220), Color.FromArgb(56, 189, 248));
            Glow(g, new Rectangle(Width - 170, 40, 260, 200), Color.FromArgb(217, 119, 87));
            using (var pen = new Pen(new System.Drawing.Drawing2D.LinearGradientBrush(r, Color.FromArgb(150, 56, 189, 248), Color.FromArgb(150, 217, 119, 87), 35f), 1.5f))
            using (var bp = Round(new Rectangle(1, 1, Width - 3, Height - 3), 21)) g.DrawPath(pen, bp);
            var tile = new Rectangle(30, 34, 40, 40);
            using (var tp = Round(tile, 11))
            using (var tb = new System.Drawing.Drawing2D.LinearGradientBrush(tile, Color.FromArgb(110, 217, 119, 87), Color.FromArgb(80, 192, 132, 252), 45f))
            using (var tpen = new Pen(Color.FromArgb(140, 217, 119, 87))) { g.FillPath(tb, tp); g.DrawPath(tpen, tp); }
            using (var f = new Font("Segoe Fluent Icons", 15)) using (var br = new SolidBrush(Color.FromArgb(246, 180, 143)))
                g.DrawString("", f, br, tile, new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center });
            using (var f = new Font("Segoe UI Variable Display", 13, FontStyle.Bold)) using (var br = new SolidBrush(Color.FromArgb(238, 241, 250)))
                g.DrawString("H E A D R O O M", f, br, 84, 34);
            using (var f = new Font("Segoe UI Variable Display", 9.5f)) using (var br = new SolidBrush(Color.FromArgb(138, 147, 178)))
                g.DrawString("Reading sensors…", f, br, 86, 58);
            var bar = new Rectangle(30, 104, Width - 60, 6);
            using (var bp = Round(bar, 3)) using (var bb = new SolidBrush(Color.FromArgb(25, 255, 255, 255))) g.FillPath(bb, bp);
            int w = 110, x = bar.X + (tick * 6) % (bar.Width + w) - w;
            var seg = Rectangle.Intersect(new Rectangle(x, bar.Y, w, bar.Height), bar);
            if (seg.Width > 4)
                using (var sp = Round(seg, 3))
                using (var sb = new System.Drawing.Drawing2D.LinearGradientBrush(new Rectangle(x, bar.Y, w, bar.Height), Color.FromArgb(194, 87, 58), Color.FromArgb(246, 180, 143), 0f)) g.FillPath(sb, sp);
        };
    }
    static void Glow(Graphics g, Rectangle r, Color c)
    {
        using (var p = new System.Drawing.Drawing2D.GraphicsPath()) { p.AddEllipse(r);
            using (var b = new System.Drawing.Drawing2D.PathGradientBrush(p) { CenterColor = Color.FromArgb(60, c), SurroundColors = new[] { Color.FromArgb(0, c) } }) g.FillPath(b, p); }
    }
    static System.Drawing.Drawing2D.GraphicsPath Round(Rectangle r, int rad)
    {
        var p = new System.Drawing.Drawing2D.GraphicsPath(); int d = rad * 2;
        p.AddArc(r.X, r.Y, d, d, 180, 90); p.AddArc(r.Right - d, r.Y, d, d, 270, 90);
        p.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90); p.AddArc(r.X, r.Bottom - d, d, d, 90, 90); p.CloseFigure(); return p;
    }
}
