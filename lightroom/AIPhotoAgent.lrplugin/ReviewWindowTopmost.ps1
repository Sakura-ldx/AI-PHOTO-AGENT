<#
Keep the AI Photo Agent floating review window above the Lightroom main window.

Lightroom Classic 15.1 creates SDK floating dialogs as a custom "AgWinFrame"
top-level window with no always-on-top flag. The main window can cover it, and a
taskbar thumbnail click does not reliably raise it again. Lightroom 15.2 fixed
that by forcing these dialogs topmost; this helper emulates the same behaviour
on 15.1 through the Win32 API.

Scope and safety:
  - only class AgWinFrame windows whose title starts with -TitlePrefix
  - topmost only while a Lightroom window is the foreground application
  - removes WS_MINIMIZEBOX so the window cannot be minimized away
  - restores the window if it was already minimized
  - never calls SetForegroundWindow, so it does not steal focus
  - exits after the window has been gone for about 6 seconds (or 6 hours)
  - no network access, no elevation, no process injection

Exit codes: 0 = ok, 2 = -Once found no window, 1 = unexpected error.
#>
[CmdletBinding()]
param(
    [string]$TitlePrefix = "AI Photo Agent",
    [int]$TimeoutMinutes = 360,
    [switch]$Once,
    [switch]$ForceTopmost,
    [string]$LogFile = ""
)

$ErrorActionPreference = "Stop"

function Write-Log([string]$Message) {
    if (-not $LogFile) { return }
    try {
        $line = "{0:yyyy-MM-dd HH:mm:ss} {1}`r`n" -f (Get-Date), $Message
        [System.IO.File]::AppendAllText($LogFile, $line, [System.Text.Encoding]::UTF8)
    } catch { }
}

$signature = @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class AipaTopmost
{
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int count);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int command);

    [DllImport("user32.dll")]
    public static extern int GetWindowLong(IntPtr hWnd, int index);

    [DllImport("user32.dll")]
    public static extern int SetWindowLong(IntPtr hWnd, int index, int newStyle);

    [DllImport("user32.dll")]
    public static extern bool SetWindowPos(
        IntPtr hWnd,
        IntPtr insertAfter,
        int x,
        int y,
        int cx,
        int cy,
        uint flags);

    public const int GWL_STYLE = -16;
    public const int GWL_EXSTYLE = -20;
    public const int WS_MINIMIZEBOX = 0x00020000;
    public const int WS_EX_TOPMOST = 0x00000008;
    public const int SW_RESTORE = 9;

    public static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
    public static readonly IntPtr HWND_NOTOPMOST = new IntPtr(-2);

    public const uint SWP_NOSIZE = 0x0001;
    public const uint SWP_NOMOVE = 0x0002;
    public const uint SWP_NOACTIVATE = 0x0010;
    public const uint SWP_FRAMECHANGED = 0x0020;

    public static IntPtr[] Find(string titlePrefix)
    {
        var found = new List<IntPtr>();
        EnumWindows(delegate(IntPtr hWnd, IntPtr lParam)
        {
            if (!IsWindowVisible(hWnd)) { return true; }

            var className = new StringBuilder(128);
            GetClassName(hWnd, className, className.Capacity);
            if (!string.Equals(className.ToString(), "AgWinFrame", StringComparison.Ordinal))
            {
                return true;
            }

            var title = new StringBuilder(512);
            GetWindowText(hWnd, title, title.Capacity);
            if (!title.ToString().StartsWith(titlePrefix, StringComparison.Ordinal))
            {
                return true;
            }

            found.Add(hWnd);
            return true;
        }, IntPtr.Zero);
        return found.ToArray();
    }

    public static bool IsTopmost(IntPtr hWnd)
    {
        return (GetWindowLong(hWnd, GWL_EXSTYLE) & WS_EX_TOPMOST) != 0;
    }

    public static void RemoveMinimizeBox(IntPtr hWnd)
    {
        int style = GetWindowLong(hWnd, GWL_STYLE);
        if ((style & WS_MINIMIZEBOX) == 0) { return; }
        SetWindowLong(hWnd, GWL_STYLE, style & ~WS_MINIMIZEBOX);
    }

    public static void SetTopmost(IntPtr hWnd, bool topmost)
    {
        SetWindowPos(
            hWnd,
            topmost ? HWND_TOPMOST : HWND_NOTOPMOST,
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED);
    }

    public static uint ProcessId(IntPtr hWnd)
    {
        uint processId;
        GetWindowThreadProcessId(hWnd, out processId);
        return processId;
    }
}
"@

Add-Type -TypeDefinition $signature -Language CSharp | Out-Null

$deadline = (Get-Date).AddMinutes([Math]::Max(1, $TimeoutMinutes))
$missing = 0
Write-Log ("helper start prefix='{0}' once={1} force={2}" -f $TitlePrefix, $Once, $ForceTopmost)

while ((Get-Date) -lt $deadline) {
    $windows = [AipaTopmost]::Find($TitlePrefix)

    if ($windows.Count -eq 0) {
        $missing++
        if ($Once) {
            Write-Log "no matching window (once)"
            exit 2
        }
        if ($missing -ge 12) {
            Write-Log "window gone for 12 ticks; exit"
            exit 0
        }
        Start-Sleep -Milliseconds 500
        continue
    }

    $missing = 0
    $foregroundPid = [AipaTopmost]::ProcessId([AipaTopmost]::GetForegroundWindow())

    foreach ($hwnd in $windows) {
        try {
            if ([AipaTopmost]::IsIconic($hwnd)) {
                [void][AipaTopmost]::ShowWindow($hwnd, [AipaTopmost]::SW_RESTORE)
                Write-Log ("restored hwnd=0x{0:X}" -f $hwnd.ToInt64())
            }

            [AipaTopmost]::RemoveMinimizeBox($hwnd)

            $hwndPid = [AipaTopmost]::ProcessId($hwnd)
            $isTopmost = [AipaTopmost]::IsTopmost($hwnd)
            $wantTopmost = $ForceTopmost -or ($foregroundPid -eq $hwndPid)

            if ($wantTopmost -and -not $isTopmost) {
                [AipaTopmost]::SetTopmost($hwnd, $true)
                Write-Log ("topmost on hwnd=0x{0:X}" -f $hwnd.ToInt64())
            }
            elseif ((-not $wantTopmost) -and $isTopmost) {
                [AipaTopmost]::SetTopmost($hwnd, $false)
                Write-Log ("topmost off hwnd=0x{0:X}" -f $hwnd.ToInt64())
            }
        }
        catch {
            Write-Log ("window error: " + $_.Exception.Message)
        }
    }

    if ($Once) {
        Write-Log "once pass done"
        exit 0
    }

    Start-Sleep -Milliseconds 500
}

Write-Log "timeout; exit"
exit 0
