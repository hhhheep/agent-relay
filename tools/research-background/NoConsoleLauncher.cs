using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

// Compile as WinExe: the launcher has no console subsystem and no UI.
// CREATE_NO_WINDOW for the Node child prevents console allocation rather than
// trying to hide a window after it has already been created.
internal static class NoConsoleLauncher
{
    [DllImport("kernel32.dll")]
    private static extern IntPtr GetConsoleWindow();

    private static void WriteAtomic(string file, string value)
    {
        string temp = file + "." + Guid.NewGuid().ToString("N") + ".tmp";
        File.WriteAllText(temp, value, new UTF8Encoding(false));
        if (File.Exists(file)) File.Replace(temp, file, null);
        else File.Move(temp, file);
    }

    private static string JsonPath(string value)
    {
        return value.Replace("\\", "\\\\").Replace("\"", "\\\"");
    }

    [STAThread]
    private static int Main()
    {
        string home = AppDomain.CurrentDomain.BaseDirectory;
        string state = Path.Combine(home, "state");
        Directory.CreateDirectory(state);
        try
        {
            // Only a nonsecret executable path is kept here. Credentials remain
            // loaded by the existing provider in the Node process.
            string config = Path.Combine(home, "node-path.txt");
            string node = File.Exists(config) ? File.ReadAllText(config).Trim()
                : @"C:\Program Files\nodejs\node.exe";
            if (!Path.IsPathRooted(node) || !File.Exists(node)) throw new Exception();
            var start = new ProcessStartInfo {
                FileName = node,
                Arguments = "--disable-warning=ExperimentalWarning \"" + Path.Combine(home, "cli.mjs") + "\" run",
                WorkingDirectory = home,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            using (var child = new Process { StartInfo = start })
            {
                // Drain output so pipes cannot block. Do not write request data
                // or provider messages to a launcher log.
                child.OutputDataReceived += delegate { };
                child.ErrorDataReceived += delegate { };
                if (!child.Start()) throw new Exception();
                child.BeginOutputReadLine();
                child.BeginErrorReadLine();
                WriteAtomic(Path.Combine(state, "launcher.json"),
                    "{\"launcher_pid\":" + Process.GetCurrentProcess().Id +
                    ",\"worker_pid\":" + child.Id +
                    ",\"launcher_console_attached\":" + (GetConsoleWindow() != IntPtr.Zero ? "true" : "false") +
                    ",\"create_no_window\":true,\"use_shell_execute\":false," +
                    "\"node\":\"" + JsonPath(node) + "\",\"started_at\":\"" + DateTime.UtcNow.ToString("o") + "\"}");
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch
        {
            WriteAtomic(Path.Combine(state, "launcher-error.json"),
                "{\"code\":\"NO_CONSOLE_LAUNCH_FAILED\",\"at\":\"" + DateTime.UtcNow.ToString("o") + "\"}");
            return 1; // no message box, popup or secret-bearing exception text
        }
    }
}
