param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$PrivateConfig,
    [Parameter(Mandatory=$true)][string]$Pythonw
)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$taskConfig = (Resolve-Path -LiteralPath $PrivateConfig).Path
$taskPython = (Resolve-Path -LiteralPath $Pythonw).Path
if (-not (Test-Path -LiteralPath (Join-Path $taskRoot 'time_logger\client_daemon.py'))) {
    throw 'Project executable missing'
}
$taskName = 'PersonalTimeLogger-ModelBatch'
$taskArguments = '-m time_logger.client_daemon --config "' + $taskConfig + '"'
$taskExisting = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($taskExisting) {
    if ($taskExisting.Actions.Execute -ne $taskPython -or $taskExisting.Actions.Arguments -ne $taskArguments -or $taskExisting.Actions.WorkingDirectory -ne $taskRoot) {
        throw 'Existing task belongs to a different configuration; nothing changed'
    }
    [PSCustomObject]@{TaskName=$taskName;Status='already_installed'} | ConvertTo-Json -Compress
    exit 0
}
$taskIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskAction = New-ScheduledTaskAction -Execute $taskPython -Argument $taskArguments -WorkingDirectory $taskRoot
$taskRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 15)
$taskLogon = New-ScheduledTaskTrigger -AtLogOn -User $taskIdentity
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskIdentity -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$taskDefinition = New-ScheduledTask -Action $taskAction -Trigger @($taskRepeat,$taskLogon) -Principal $taskPrincipal -Settings $taskSettings -Description 'Personal time logger: private local model batch and offsite backup. Cloud capture runs independently.'
Register-ScheduledTask -TaskName $taskName -InputObject $taskDefinition | Out-Null
[PSCustomObject]@{TaskName=$taskName;Status='installed';IntervalMinutes=15;RunOnlyWhenUserSignedIn=$true;WindowsPasswordStored=$false} | ConvertTo-Json -Compress
