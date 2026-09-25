using System.Diagnostics;
using System.Runtime.InteropServices;

namespace LiveCams;

/// <summary>
/// Runs the Python animal tracker as a hidden child process: starts it with the
/// wallpaper, restarts it if it dies (with a back-off), and stops it on exit.
/// </summary>
internal sealed class TrackerProcess
{
    private static readonly TimeSpan RestartDelay = TimeSpan.FromMinutes(1);

    // A job object that kills the tracker if LiveCams dies, even if it's killed outright.
    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
        public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string? name);

    [DllImport("kernel32.dll")]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION info, int size);

    [DllImport("kernel32.dll")]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    private const int JobObjectExtendedLimitInformation = 9;
    private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;

    private static readonly IntPtr KillOnExitJob = CreateKillOnExitJob();

    private static IntPtr CreateKillOnExitJob()
    {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(job, JobObjectExtendedLimitInformation, ref info, Marshal.SizeOf(info));
        return job; // deliberately never closed: the OS closes it when LiveCams exits
    }

    private readonly string python;
    private readonly string script;
    private readonly string workDir;
    private readonly bool publish;
    private Process? process;
    private DateTime lastStart = DateTime.MinValue;

    public TrackerProcess(TrackerConfig config, string configDir)
    {
        python = Path.GetFullPath(Path.Combine(configDir, config.Python));
        script = Path.GetFullPath(Path.Combine(configDir, config.Script));
        workDir = Path.GetDirectoryName(script)!;
        publish = config.PublishResults;
    }

    public int? ProcessId => process is { HasExited: false } p ? p.Id : null;

    public void Start()
    {
        if (!File.Exists(python) || !File.Exists(script))
        {
            Log.Write($"tracker not started: missing {(File.Exists(python) ? script : python)}");
            return;
        }
        lastStart = DateTime.UtcNow;
        try
        {
            string args = $"\"{script}\"" + (publish ? " --publish" : "");
            process = Process.Start(new ProcessStartInfo(python, args)
            {
                WorkingDirectory = workDir,
                UseShellExecute = false,
                CreateNoWindow = true,
            });
            if (process != null)
            {
                AssignProcessToJobObject(KillOnExitJob, process.Handle);
                Native.SetEfficiencyMode(process.Id);
                Log.Write($"tracker started (pid {process.Id})");
            }
        }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            Log.Write($"tracker failed to start: {ex.Message}");
        }
    }

    /// <summary>Called every tick: restarts the tracker if it has exited.</summary>
    public void KeepAlive()
    {
        if (process is { HasExited: false }) return;
        if (DateTime.UtcNow - lastStart < RestartDelay) return;
        if (process != null) Log.Write($"tracker exited (code {process.ExitCode}); restarting");
        Start();
    }

    public void Stop()
    {
        if (process is not { HasExited: false }) return;
        try
        {
            process.Kill(entireProcessTree: true);
            process.WaitForExit(5000);
            Log.Write("tracker stopped");
        }
        catch (InvalidOperationException)
        {
            // already gone
        }
    }
}
