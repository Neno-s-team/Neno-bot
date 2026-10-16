$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root 'venv\Scripts\pythonw.exe'
$Script = Join-Path $Root 'scripts\backup_backend.py'
if (!(Test-Path -LiteralPath $Python) -or !(Test-Path -LiteralPath $Script)) {
    throw 'Missing backup runtime or script'
}
$Name = 'Neno-Backend-Backup'
$Existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
if ($Existing -and !($Existing.Actions | Where-Object {$_.Arguments -like '*backup_backend.py*'})) {
    throw 'A different task already uses Neno-Backend-Backup; refusing to replace it'
}
$Action = New-ScheduledTaskAction -Execute $Python -Argument ('-X utf8 "' + $Script + '"') -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Daily -At '04:20'
$Principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'Daily Neno source, configuration, SQLite and media snapshot; TDLib session uses separate cold snapshots.' -Force | Select-Object TaskName,State
