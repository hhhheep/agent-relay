param(
  [ValidateSet('Install','Start','Stop','Status','Uninstall','Run')]
  [string]$Action = 'Status',
  [string]$NodePath = 'C:\Program Files\nodejs\node.exe'
)
$ErrorActionPreference = 'Stop'
$taskName = 'AgentRelay-Development'
$cliPath = Join-Path $PSScriptRoot 'cli.mjs'
$statePath = Join-Path $PSScriptRoot 'state'
$stopPath = Join-Path $statePath 'STOP'
$launcherPath = Join-Path $PSScriptRoot 'launch-hidden.vbs'
$startupPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'AgentRelay-Development.vbs'
$installPath = Join-Path $statePath 'installation.json'
$noConsolePath = Join-Path $PSScriptRoot 'NoConsoleLauncher.exe'
function Build-NoConsoleLauncher {
  $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
  $source = Join-Path $PSScriptRoot 'NoConsoleLauncher.cs'
  if (-not (Test-Path -LiteralPath $compiler)) { throw 'Windows .NET compiler unavailable' }
  $compile = [Diagnostics.ProcessStartInfo]::new()
  $compile.FileName = $compiler
  $compile.Arguments = '/nologo /target:winexe /optimize+ /out:"' + $noConsolePath + '" "' + $source + '"'
  $compile.UseShellExecute = $false
  $compile.CreateNoWindow = $true
  $compile.RedirectStandardOutput = $true
  $compile.RedirectStandardError = $true
  $builder = [Diagnostics.Process]::Start($compile)
  $out = $builder.StandardOutput.ReadToEnd()
  $err = $builder.StandardError.ReadToEnd()
  $builder.WaitForExit()
  if ($builder.ExitCode -ne 0) { throw ('Launcher build failed: ' + $out + $err) }
  [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'node-path.txt'),$NodePath,[Text.UTF8Encoding]::new($false))
}
function Start-HiddenWorker {
  if (Test-Path -LiteralPath $stopPath) { Remove-Item -LiteralPath $stopPath }
  $start = [Diagnostics.ProcessStartInfo]::new()
  $start.FileName = $noConsolePath
  $start.WorkingDirectory = $PSScriptRoot
  $start.UseShellExecute = $false
  $start.CreateNoWindow = $true
  $p = [Diagnostics.Process]::Start($start)
  return $p.Id
}
function Invoke-NoConsoleNode([string]$Command) {
  $start = [Diagnostics.ProcessStartInfo]::new()
  $start.FileName = $NodePath
  $start.Arguments = '--disable-warning=ExperimentalWarning "' + $cliPath + '" ' + $Command
  $start.UseShellExecute = $false
  $start.CreateNoWindow = $true
  $start.RedirectStandardOutput = $true
  $start.RedirectStandardError = $true
  $p = [Diagnostics.Process]::Start($start)
  $out = $p.StandardOutput.ReadToEnd()
  $err = $p.StandardError.ReadToEnd()
  $p.WaitForExit()
  if ($out.Trim()) { Write-Output $out.Trim() }
  if ($p.ExitCode -ne 0) { throw ('Node command failed: ' + $err.Trim()) }
}
switch ($Action) {
  'Run' { $startedPid = Start-HiddenWorker; Wait-Process -Id $startedPid }
  'Install' {
    New-Item -ItemType Directory -Path $statePath -Force | Out-Null
    # Persist baseline before installing any automatic trigger.
    Invoke-NoConsoleNode 'baseline'
    Build-NoConsoleLauncher
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    try {
      $taskAction = New-ScheduledTaskAction -Execute $noConsolePath -WorkingDirectory $PSScriptRoot
      $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
      $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
      $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
      Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings -Description 'Serial local research summaries using existing Bailian DS credentials; no Codex or Obsidian dependency.' -Force | Out-Null
      @{mode='scheduled_task';task=$taskName;installed_at=(Get-Date).ToString('o');node=$NodePath;launcher='WinExe_CREATE_NO_WINDOW'} | ConvertTo-Json | Set-Content -LiteralPath $installPath -Encoding UTF8
      if (Test-Path -LiteralPath $startupPath) { Remove-Item -LiteralPath $startupPath }
      if (Test-Path -LiteralPath $stopPath) { Remove-Item -LiteralPath $stopPath }
      Start-ScheduledTask -TaskName $taskName
    } catch {
      # Windows user Startup is a native per-login fallback when scheduler ACL
      # denies registration. No administrator, platform or service is installed.
      $registered = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
      if ($registered) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue }
      # WSH requires UTF-16 or system ANSI, not BOM-less UTF-8 for CJK paths.
      Get-Content -LiteralPath $launcherPath -Raw -Encoding UTF8 | Set-Content -LiteralPath $startupPath -Encoding Unicode
      @{mode='startup_vbs';launcher=$startupPath;installed_at=(Get-Date).ToString('o');node=$NodePath;scheduler_error='REGISTRATION_UNAVAILABLE'} | ConvertTo-Json | Set-Content -LiteralPath $installPath -Encoding UTF8
      $pidStarted = Start-HiddenWorker
      Write-Output "Startup fallback installed; worker PID $pidStarted"
    }
  }
  'Start' {
    if (Test-Path -LiteralPath $stopPath) { Remove-Item -LiteralPath $stopPath }
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) { Enable-ScheduledTask -TaskName $taskName | Out-Null; Start-ScheduledTask -TaskName $taskName }
    else { Write-Output ('Started worker PID ' + (Start-HiddenWorker)) }
  }
  'Stop' {
    [IO.Directory]::CreateDirectory($statePath) | Out-Null
    [IO.File]::WriteAllText($stopPath,'Stop requested',[Text.UTF8Encoding]::new($false))
    # Cooperative stop leaves an interrupted API attempt recoverable. Logon
    # launch respects persistent STOP until an explicit Start clears it.
  }
  'Status' {
    if (Test-Path -LiteralPath $installPath) { Get-Content -LiteralPath $installPath }
    Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Select-Object TaskName,State
    Write-Output ('Startup launcher exists: ' + (Test-Path -LiteralPath $startupPath))
    Invoke-NoConsoleNode 'status'
  }
  'Uninstall' {
    [IO.File]::WriteAllText($stopPath,'Stop requested',[Text.UTF8Encoding]::new($false))
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    if (Test-Path -LiteralPath $startupPath) { Remove-Item -LiteralPath $startupPath }
    if (Test-Path -LiteralPath $installPath) { Remove-Item -LiteralPath $installPath }
    Write-Output 'Autostart removed. Sources, generated summaries and SQLite state retained.'
  }
}
