// Headroom installer / uninstaller. One exe: the app (with its own Python runtime) is an embedded zip.
//   HeadroomSetup.exe                 install with UI
//   HeadroomSetup.exe /S [/D=dir]     silent install
//   HeadroomSetup.exe /extract dir    unpack only (testing; no shortcuts, registry or tasks)
//   Uninstall.exe /uninstall [/S] [/removedata]
// Built by installer\build.ps1 with the .NET Framework C# compiler that ships with Windows.
using System;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

static class App
{
    public const string Name = "Headroom";
    public const string UninstKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\Headroom";
    public static readonly string[] Tasks = { "Headroom GPU Apply", "Headroom Fan Curve", "Headroom Resume", "Headroom Sensors",
                                              "Headroom CPU Watchdog", "Headroom Crash Watch (System)", "Headroom Crash Watch (Application)",
                                              "Headroom Research", @"\CoreCycler\CoreCycler AutoMode Startup Task" };
    public static string Version = "1.0.0";
    public static string DefaultDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), Name);

    [DllImport("kernel32.dll")] static extern bool SetDefaultDllDirectories(uint flags);
    [DllImport("user32.dll")] static extern IntPtr FindWindow(string cls, string title);
    [DllImport("user32.dll")] static extern IntPtr PostMessage(IntPtr h, int msg, IntPtr w, IntPtr l);

    [STAThread]
    static int Main(string[] args)
    {
        // an installer usually runs from Downloads: never load DLLs from the folder it sits in
        try { SetDefaultDllDirectories(0x00000800); } catch { }
        try
        {
            using (var s = Assembly.GetExecutingAssembly().GetManifestResourceStream("version.txt"))
                if (s != null) Version = new StreamReader(s).ReadToEnd().Trim();
        }
        catch { }
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        string dir = null; bool silent = false, uninstall = false, removeData = false; string extract = null;
        for (int i = 0; i < args.Length; i++)
        {
            var a = args[i];
            if (a.Equals("/S", StringComparison.OrdinalIgnoreCase)) silent = true;
            else if (a.StartsWith("/D=", StringComparison.OrdinalIgnoreCase)) dir = a.Substring(3);
            else if (a.Equals("/uninstall", StringComparison.OrdinalIgnoreCase)) uninstall = true;
            else if (a.Equals("/removedata", StringComparison.OrdinalIgnoreCase)) removeData = true;
            else if (a.Equals("/dir", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length) dir = args[++i];
            else if (a.Equals("/extract", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length) extract = args[++i];
        }
        try
        {
            if (extract != null) { Installer.Extract(extract, null); return 0; }
            if (uninstall) return Uninstaller.Run(dir, silent, removeData);
            if (silent) { Installer.Install(dir ?? DefaultDir, false, false, null); return 0; }
            Application.Run(new SetupForm());
            return 0;
        }
        catch (Exception e)
        {
            if (!silent) MessageBox.Show(e.Message, "Headroom Setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    public static void Stop(string dir)
    {
        // close a running Headroom window the normal way: it stops any test and shuts its backend down
        var h = FindWindow(null, "Headroom");
        if (h != IntPtr.Zero)
        {
            PostMessage(h, 0x0010 /* WM_CLOSE */, IntPtr.Zero, IntPtr.Zero);
            for (int i = 0; i < 50 && Process.GetProcessesByName("Headroom").Length > 0; i++) Thread.Sleep(500);
        }
        // ask a running Headroom to shut down cleanly (it puts the fan curve process to rest too). The API needs the
        // launcher's token, so use the backend's named event (only elevated processes can signal it).
        // a backend without a window (or one that didn't close): its admin-only named event
        try { EventWaitHandle ev; if (EventWaitHandle.TryOpenExisting(@"Local\Headroom.Shutdown", out ev)) using (ev) ev.Set(); } catch { }
        Thread.Sleep(1500);
        foreach (var p in Process.GetProcesses())
        {
            try
            {
                string path = null;
                try { path = p.MainModule.FileName; } catch { }
                bool inDir = path != null && dir != null && path.StartsWith(dir.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase);
                if (p.ProcessName.Equals("Headroom", StringComparison.OrdinalIgnoreCase) || inDir) { p.Kill(); p.WaitForExit(5000); }
            }
            catch { }
        }
    }

    public static string Run(string exe, string args, int waitMs = 30000)
    {
        try
        {
            var p = Process.Start(new ProcessStartInfo(exe, args) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true });
            string o = p.StandardOutput.ReadToEnd(); p.WaitForExit(waitMs); return o;
        }
        catch { return ""; }
    }

    public static void Shortcut(string lnk, string target, string workDir, string desc)
    {
        var t = Type.GetTypeFromProgID("WScript.Shell");
        object sh = Activator.CreateInstance(t);
        object sc = t.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, sh, new object[] { lnk });
        var st = sc.GetType();
        st.InvokeMember("TargetPath", BindingFlags.SetProperty, null, sc, new object[] { target });
        st.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, sc, new object[] { workDir });
        st.InvokeMember("Description", BindingFlags.SetProperty, null, sc, new object[] { desc });
        st.InvokeMember("IconLocation", BindingFlags.SetProperty, null, sc, new object[] { target + ",0" });
        st.InvokeMember("Save", BindingFlags.InvokeMethod, null, sc, null);
    }

    public static string StartMenuLnk { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonPrograms), "Headroom.lnk"); } }
    public static string DesktopLnk { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonDesktopDirectory), "Headroom.lnk"); } }
}

static class Acl
{
    static readonly SecurityIdentifier Admins = new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null);
    static readonly SecurityIdentifier System_ = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
    static readonly SecurityIdentifier Users = new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid, null);

    // Only admins can change the folder; users may read and run. Headroom runs elevated from here,
    // so a folder ordinary programs could write to would let them gain admin rights.
    public static void Lock(string dir, bool usersRead)
    {
        var ds = new DirectorySecurity();
        ds.SetOwner(Admins);
        ds.SetAccessRuleProtection(true, false);
        var inh = InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit;
        ds.AddAccessRule(new FileSystemAccessRule(Admins, FileSystemRights.FullControl, inh, PropagationFlags.None, AccessControlType.Allow));
        ds.AddAccessRule(new FileSystemAccessRule(System_, FileSystemRights.FullControl, inh, PropagationFlags.None, AccessControlType.Allow));
        if (usersRead)
            ds.AddAccessRule(new FileSystemAccessRule(Users, FileSystemRights.ReadAndExecute, inh, PropagationFlags.None, AccessControlType.Allow));
        Directory.SetAccessControl(dir, ds);
    }
}

static class Installer
{
    static readonly string[] AppDirs = { "headroom", "web", "profiles", "runtime", "vendor", "bin" };

    public const string ManifestName = "install.manifest";
    // files a 1.0.0 install put in the folder (it had no manifest)
    public static readonly string[] Legacy = { "headroom", "web", "profiles", "runtime", "vendor", "Headroom.exe", "Uninstall.exe",
        "Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll", "WebView2Loader.dll", "headroom.ico", "headroom.png",
        "headroom_server.pyw", "headroom_fans.pyw", "headroom_apply.pyw", "headroom_reset.pyw", "LICENSE", "THIRD_PARTY_NOTICES.md",
        "README.md", "version.txt" };

    /// <summary>Only install into a new/empty folder or over an existing Headroom (uninstall deletes what it installed).</summary>
    public static void CheckTarget(string dir)
    {
        if (!Directory.Exists(dir) || Directory.GetFileSystemEntries(dir).Length == 0) return;
        if (File.Exists(Path.Combine(dir, ManifestName)) || File.Exists(Path.Combine(dir, "Headroom.exe"))) return;
        throw new Exception("The folder " + dir + " already has other files in it. Pick an empty folder or a new 'Headroom' folder.");
    }

    public static void Extract(string dir, Action<int, string> progress)
    {
        Directory.CreateDirectory(dir);
        foreach (var d in AppDirs)
        {   // remove the previous version's code so no stale modules are left behind
            var p = Path.Combine(dir, d);
            if (Directory.Exists(p)) Directory.Delete(p, true);
        }
        var top = new System.Collections.Generic.SortedSet<string>(StringComparer.OrdinalIgnoreCase) { "Uninstall.exe", ManifestName, "tools" };
        using (var s = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
        using (var z = new ZipArchive(s, ZipArchiveMode.Read))
        {
            int n = 0, total = z.Entries.Count;
            foreach (var e in z.Entries) { var nm = e.FullName.Replace('\\', '/').TrimStart('/'); if (nm.Length > 0) top.Add(nm.Split('/')[0]); }
            foreach (var e in z.Entries)
            {
                var name = e.FullName.Replace('\\', '/');
                var dest = Path.GetFullPath(Path.Combine(dir, name.TrimEnd('/')));
                if (!dest.StartsWith(Path.GetFullPath(dir).TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase)) continue;
                if (name.EndsWith("/")) { Directory.CreateDirectory(dest); continue; }
                Directory.CreateDirectory(Path.GetDirectoryName(dest));
                e.ExtractToFile(dest, true);
                if (progress != null && (++n % 40 == 0 || n == total)) progress(5 + 80 * n / total, "Copying files (" + n + " / " + total + ")");
            }
        }
        // 1.0.0 kept its entry scripts in the root; they moved to bin\
        foreach (var old in new[] { "headroom_server.pyw", "headroom_fans.pyw", "headroom_apply.pyw", "headroom_reset.pyw" })
            if (!top.Contains(old)) try { File.Delete(Path.Combine(dir, old)); } catch { }
        File.WriteAllLines(Path.Combine(dir, ManifestName), top);
    }

    public static void Install(string dir, bool desktop, bool launch, Action<int, string> progress)
    {
        progress = progress ?? ((p, m) => { });
        dir = Path.GetFullPath(dir);
        CheckTarget(dir);
        progress(2, "Closing Headroom if it's running");
        App.Stop(dir);
        var oldPy = Path.Combine(dir, @"runtime\python.exe");   // upgrade: the fan curve process was stopped, hand fans back
        foreach (var reset in new[] { Path.Combine(dir, @"bin\headroom_reset.pyw"), Path.Combine(dir, "headroom_reset.pyw") })
            if (File.Exists(oldPy) && File.Exists(reset)) { App.Run(oldPy, "\"" + reset + "\" --fans-only", 20000); break; }
        Directory.CreateDirectory(dir);
        Acl.Lock(dir, true);   // before any file is written (also a custom folder picked with /D= or Browse)
        Extract(dir, progress);
        MigrateTasks(dir);
        progress(88, "Creating shortcuts");
        var self = Assembly.GetExecutingAssembly().Location;
        var uninst = Path.Combine(dir, "Uninstall.exe");
        if (!self.Equals(uninst, StringComparison.OrdinalIgnoreCase)) File.Copy(self, uninst, true);
        var exe = Path.Combine(dir, "Headroom.exe");
        App.Shortcut(App.StartMenuLnk, exe, dir, "Headroom - safe auto overclocking");
        if (desktop) App.Shortcut(App.DesktopLnk, exe, dir, "Headroom - safe auto overclocking");
        progress(94, "Registering the uninstaller");
        using (var hk = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64))
        using (var k = hk.CreateSubKey(App.UninstKey))
        {
            k.SetValue("DisplayName", "Headroom");
            k.SetValue("DisplayVersion", App.Version);
            k.SetValue("Publisher", "Headroom");
            k.SetValue("DisplayIcon", exe + ",0");
            k.SetValue("InstallLocation", dir);
            k.SetValue("UninstallString", "\"" + uninst + "\" /uninstall");
            k.SetValue("QuietUninstallString", "\"" + uninst + "\" /uninstall /S");
            k.SetValue("NoModify", 1, RegistryValueKind.DWord);
            k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
            k.SetValue("EstimatedSize", (int)(DirSize(dir) / 1024), RegistryValueKind.DWord);
        }
        // an upgrade keeps the user's fan curve running if it was set to start at logon
        if (App.Run("schtasks.exe", "/query /tn \"Headroom Fan Curve\"", 10000).Contains("Headroom Fan Curve"))
            App.Run("schtasks.exe", "/run /tn \"Headroom Fan Curve\"", 10000);
        // an upgrade stopped everything running from this folder: start the sensor app again too (one copy)
        if (App.Run("schtasks.exe", "/query /tn \"Headroom Sensors\"", 10000).Contains("Headroom Sensors"))
            App.Run("schtasks.exe", "/run /tn \"Headroom Sensors\"", 10000);
        progress(100, "Done");
        if (launch) Process.Start(new ProcessStartInfo(exe) { WorkingDirectory = dir });
    }

    /// <summary>Re-point Headroom's logon tasks at the scripts' current location (an upgrade from 1.0.0 moved them to bin\).</summary>
    static void MigrateTasks(string dir)
    {
        var pyw = Path.Combine(dir, @"runtime\pythonw.exe");
        var map = new[] { new[] { "Headroom GPU Apply", "headroom_apply.pyw", "/delay 0001:00" }, new[] { "Headroom Fan Curve", "headroom_fans.pyw", "" } };
        foreach (var t in map)
        {
            if (!App.Run("schtasks.exe", "/query /tn \"" + t[0] + "\"", 10000).Contains(t[0])) continue;
            var tr = "\\\"" + pyw + "\\\" \\\"" + Path.Combine(dir, @"bin\" + t[1]) + "\\\"";
            App.Run("schtasks.exe", "/create /f /rl highest /sc onlogon /tn \"" + t[0] + "\" /tr \"" + tr + "\" " + t[2], 10000);
        }
    }

    static long DirSize(string d)
    {
        long n = 0;
        try { foreach (var f in Directory.GetFiles(d, "*", SearchOption.AllDirectories)) n += new FileInfo(f).Length; } catch { }
        return n;
    }

    public static bool WebView2Present()
    {
        foreach (var key in new[] { @"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
                                    @"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" })
        {
            foreach (var hive in new[] { Registry.LocalMachine, Registry.CurrentUser })
                using (var k = hive.OpenSubKey(key))
                    if (k != null && (k.GetValue("pv") as string ?? "0.0.0.0") != "0.0.0.0") return true;
        }
        return false;
    }
}

static class Uninstaller
{
    /// <summary>Delete only what Headroom installed (its manifest, or 1.0.0's known files), then the folder if it's empty.
    /// Never a blind recursive delete: the folder may have been chosen by hand and hold other things.</summary>
    static void RemoveInstalled(string dir)
    {
        if (!File.Exists(Path.Combine(dir, "Headroom.exe")) && !File.Exists(Path.Combine(dir, Installer.ManifestName))) return;
        var mf = Path.Combine(dir, Installer.ManifestName);
        var names = File.Exists(mf) ? File.ReadAllLines(mf) : Installer.Legacy;
        var root = Path.GetFullPath(dir).TrimEnd('\\') + "\\";
        for (int i = 0; i < 5; i++)
        {
            bool left = false;
            foreach (var n in names)
            {
                if (string.IsNullOrWhiteSpace(n) || n.Contains("..") || n.Contains(":") || n.Contains("\\") || n.Contains("/")) continue;
                var p = Path.GetFullPath(Path.Combine(dir, n.Trim()));
                if (!p.StartsWith(root, StringComparison.OrdinalIgnoreCase) || p.Equals(mf, StringComparison.OrdinalIgnoreCase)) continue;
                try { if (Directory.Exists(p)) Directory.Delete(p, true); else if (File.Exists(p)) File.Delete(p); } catch { left = true; }
            }
            if (!left) break;
            Thread.Sleep(1000);
        }
        try { File.Delete(mf); } catch { }
        try { if (Directory.GetFileSystemEntries(dir).Length == 0) Directory.Delete(dir); } catch { }
    }

    public static int Run(string dir, bool silent, bool removeData)
    {
        if (dir == null)
        {
            dir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            // can't delete our own folder while running from it: continue from a temp copy, in a
            // folder only admins can write to (the user's %TEMP% would let other programs plant DLLs)
            var tdir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "Temp", "HeadroomUninstall-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(tdir);
            Acl.Lock(tdir, false);
            var tmp = Path.Combine(tdir, "HeadroomUninstall.exe");
            File.Copy(Assembly.GetExecutingAssembly().Location, tmp, true);
            Process.Start(new ProcessStartInfo(tmp, "/uninstall /dir \"" + dir + "\"" + (silent ? " /S" : "") + (removeData ? " /removedata" : "")) { UseShellExecute = false });
            return 0;
        }
        if (!File.Exists(Path.Combine(dir, "Headroom.exe")) || !(File.Exists(Path.Combine(dir, @"bin\headroom_server.pyw")) || File.Exists(Path.Combine(dir, "headroom_server.pyw"))))
            throw new InvalidOperationException("No Headroom installation found in " + dir);   // never delete some other folder
        if (!silent)
        {
            var r = MessageBox.Show("Remove Headroom?\n\nThe GPU goes back to stock clocks and the fans back to the card's own control. " +
                                    "CPU settings you entered in the BIOS stay there (set PBO / Curve Optimizer back to Auto to undo them).",
                                    "Uninstall Headroom", MessageBoxButtons.OKCancel, MessageBoxIcon.Question);
            if (r != DialogResult.OK) return 1;
            removeData = MessageBox.Show("Also delete Headroom's settings, results and logs?\n\n(Yes = remove everything, No = keep them for a reinstall)",
                                         "Uninstall Headroom", MessageBoxButtons.YesNo, MessageBoxIcon.Question) == DialogResult.Yes;
        }
        App.Stop(dir);
        var py = Path.Combine(dir, @"runtime\python.exe");
        foreach (var reset in new[] { Path.Combine(dir, @"bin\headroom_reset.pyw"), Path.Combine(dir, "headroom_reset.pyw") })
            if (File.Exists(py) && File.Exists(reset)) { App.Run(py, "\"" + reset + "\"", 30000); break; }
        foreach (var t in App.Tasks) App.Run("schtasks.exe", "/delete /f /tn \"" + t + "\"", 10000);
        var local = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Headroom");
        App.Stop(Path.Combine(local, "tools"));   // tools downloaded by 1.0.0 (newer versions keep them in <dir>\tools)
        try { File.Delete(App.StartMenuLnk); } catch { }
        try { File.Delete(App.DesktopLnk); } catch { }
        using (var hk = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64)) { try { hk.DeleteSubKeyTree(App.UninstKey, false); } catch { } }
        RemoveInstalled(dir);
        if (removeData)
        {
            var roaming = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Headroom");
            foreach (var d in new[] { roaming, local }) { try { if (Directory.Exists(d)) Directory.Delete(d, true); } catch { } }
        }
        if (!silent) MessageBox.Show(Directory.Exists(dir) ? "Headroom was removed, but some files in " + dir + " were in use. Delete the folder after a restart."
                                                           : "Headroom was removed.", "Uninstall Headroom", MessageBoxButtons.OK, MessageBoxIcon.Information);
        return 0;
    }
}

class SetupForm : Form
{
    [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr h, int attr, ref int val, int size);
    static readonly Color Bg = Color.FromArgb(14, 18, 34), Panel = Color.FromArgb(24, 30, 50), Text1 = Color.FromArgb(238, 241, 250),
                          Dim = Color.FromArgb(138, 147, 178), Accent = Color.FromArgb(217, 119, 87), Cyan = Color.FromArgb(56, 189, 248);
    TextBox path; CheckBox desk, launch, agree; Button go, cancel, browse; ProgressBar bar; Label status;

    public SetupForm()
    {
        AutoScaleDimensions = new SizeF(96f, 96f); AutoScaleMode = AutoScaleMode.Dpi;   // laid out at 100%, scaled by WinForms
        Text = "Headroom Setup"; ClientSize = new Size(620, 470); FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen; BackColor = Bg; ForeColor = Text1; Font = new Font("Segoe UI", 9.5f);
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        Paint += (s, e) =>
        {
            var g = e.Graphics; g.SmoothingMode = SmoothingMode.AntiAlias; float k = g.DpiX / 96f;
            using (var br = new LinearGradientBrush(new Rectangle(0, 0, ClientSize.Width, (int)(4 * k)), Cyan, Accent, 0f)) g.FillRectangle(br, 0, 0, ClientSize.Width, 4 * k);
            using (var f = new Font("Segoe UI Semibold", 20f)) g.DrawString("HEADROOM", f, new SolidBrush(Text1), 26 * k, 22 * k);
            using (var f = new Font("Segoe UI", 10f)) g.DrawString("Safe auto overclocking  ·  version " + App.Version, f, new SolidBrush(Dim), 30 * k, 62 * k);
        };
        var intro = Lbl("Headroom finds a stable GPU overclock with its own stress tests, runs a GPU fan curve, and guides an AMD Ryzen " +
                        "Curve Optimizer tune. After installing, it scans this PC and builds a profile for your exact parts.", 30, 98, 560, 46, Text1);
        Lbl("Install to", 30, 156, 200, 20, Dim);
        path = new TextBox { Left = 30, Top = 178, Width = 470, Text = App.DefaultDir, BackColor = Panel, ForeColor = Text1, BorderStyle = BorderStyle.FixedSingle };
        browse = Btn("Browse", 508, 176, 82, false);
        browse.Click += (s, e) => { using (var d = new FolderBrowserDialog { SelectedPath = path.Text }) if (d.ShowDialog(this) == DialogResult.OK) path.Text = Path.Combine(d.SelectedPath, d.SelectedPath.EndsWith("Headroom") ? "" : "Headroom"); };
        desk = Chk("Desktop shortcut", 30, 220, true);
        launch = Chk("Start Headroom when setup finishes (runs the hardware scan)", 30, 248, true);
        var warn = Lbl("Overclocking runs hardware outside its factory settings. Headroom never raises voltages or power limits and stops at " +
                       "conservative temperatures, but a crash, a freeze or lost work during testing is possible, and some makers don't cover " +
                       "overclocking under warranty.", 30, 288, 560, 62, Dim);
        agree = Chk("I understand and use Headroom at my own risk", 30, 352, false);
        bar = new ProgressBar { Left = 30, Top = 392, Width = 560, Height = 8, Visible = false, Style = ProgressBarStyle.Continuous };
        status = Lbl("", 30, 404, 380, 20, Dim);
        go = Btn("Install", 410, 420, 90, true); cancel = Btn("Cancel", 508, 420, 82, false);
        go.Enabled = false; agree.CheckedChanged += (s, e) => go.Enabled = agree.Checked;
        cancel.Click += (s, e) => Close();
        go.Click += (s, e) => DoInstall();
        Controls.AddRange(new Control[] { path, bar });
        AcceptButton = go; CancelButton = cancel;
        if (!Installer.WebView2Present())
            status.Text = "Note: Microsoft Edge WebView2 Runtime wasn't found; Headroom will ask for it.";
    }

    protected override void OnHandleCreated(EventArgs e) { base.OnHandleCreated(e); int dark = 1; DwmSetWindowAttribute(Handle, 20, ref dark, 4); }

    Label Lbl(string t, int x, int y, int w, int h, Color c)
    {
        var l = new Label { Text = t, Left = x, Top = y, Width = w, Height = h, ForeColor = c, BackColor = Color.Transparent };
        Controls.Add(l); return l;
    }
    CheckBox Chk(string t, int x, int y, bool on)
    {
        var c = new CheckBox { Text = t, Left = x, Top = y, Width = 560, Height = 24, Checked = on, ForeColor = Text1, BackColor = Color.Transparent };
        Controls.Add(c); return c;
    }
    Button Btn(string t, int x, int y, int w, bool primary)
    {
        var b = new Button { Text = t, Left = x, Top = y, Width = w, Height = 32, FlatStyle = FlatStyle.Flat, ForeColor = primary ? Color.White : Text1,
                             BackColor = primary ? Accent : Panel, Cursor = Cursors.Hand };
        b.FlatAppearance.BorderColor = primary ? Accent : Color.FromArgb(60, 68, 96);
        Controls.Add(b); return b;
    }

    void DoInstall()
    {
        var dir = path.Text.Trim();
        if (dir.Length < 4) { MessageBox.Show(this, "Pick a folder to install to.", Text); return; }
        foreach (Control c in Controls) if (c is Button || c is CheckBox || c is TextBox) c.Enabled = false;
        bar.Visible = true;
        bool d = desk.Checked, l = launch.Checked;
        var t = new Thread(() =>
        {
            try
            {
                Installer.Install(dir, d, l, (p, m) => BeginInvoke((Action)(() => { bar.Value = Math.Min(100, p); status.Text = m; })));
                BeginInvoke((Action)(() =>
                {
                    status.Text = l ? "Installed. Headroom is starting and will scan your hardware." : "Installed. Start Headroom from the Start menu.";
                    cancel.Text = "Close"; cancel.Enabled = true;
                }));
            }
            catch (Exception ex)
            {
                BeginInvoke((Action)(() =>
                {
                    MessageBox.Show(this, "Setup failed: " + ex.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error);
                    cancel.Enabled = true;
                }));
            }
        }) { IsBackground = true };
        t.Start();
    }
}
