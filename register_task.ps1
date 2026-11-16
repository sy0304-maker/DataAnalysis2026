# 작업 스케줄러에 수집기 등록: 1분마다 collector.py --tick 실행 (collect_config.json 의 end_date 까지)
# 사용법: powershell -ExecutionPolicy Bypass -File .\register_task.ps1
# 해제:   Unregister-ScheduledTask -TaskName DataAnalysis2026-collector -Confirm:$false
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$py = (& py -c "import sys; print(sys.executable)").Trim()
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'   # 콘솔 창이 뜨지 않는 실행기
$script = Join-Path $PSScriptRoot 'collector.py'
$cfg = Get-Content (Join-Path $PSScriptRoot 'collect_config.json') -Raw -Encoding UTF8 | ConvertFrom-Json

$start = (Get-Date).Date.AddHours((Get-Date).Hour).AddMinutes((Get-Date).Minute + 1)  # 다음 정각 분
$end = [datetime]::Parse($cfg.end_date).AddDays(1)

$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$script`" --tick" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration ($end - $start)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3)

Register-ScheduledTask -TaskName 'DataAnalysis2026-collector' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "등록 완료: $start 부터 $end 까지 1분마다 실행" -ForegroundColor Green
Write-Host "로그: data\_log\collector.log" -ForegroundColor DarkGray
