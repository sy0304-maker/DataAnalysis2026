# 서울 실시간 지하철 도착정보 API 시범 호출
# 사용법: powershell -ExecutionPolicy Bypass -File .\test_realtime.ps1
[Console]::OutputEncoding = [Text.Encoding]::UTF8

# .env 에서 키 읽기
$envFile = Join-Path $PSScriptRoot '.env'
$key = $null
foreach ($line in [IO.File]::ReadAllLines($envFile, [Text.Encoding]::UTF8)) {
    if ($line -match '^\s*SEOUL_SUBWAY_REALTIME_KEY\s*=\s*(.+?)\s*$') { $key = $Matches[1] }
}
if (-not $key -or $key -like '여기에*') {
    Write-Host '.env 파일의 SEOUL_SUBWAY_REALTIME_KEY 에 실시간 지하철 키를 넣어주세요.' -ForegroundColor Red
    exit 1
}

$stations = @('신도림', '신촌', '구일', '서강대')

foreach ($st in $stations) {
    Write-Host "`n===== $st =====" -ForegroundColor Cyan
    $url = "http://swopenapi.seoul.go.kr/api/subway/$key/json/realtimeStationArrival/0/20/" + [uri]::EscapeDataString($st)
    try {
        $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 15
        $bytes = $resp.RawContentStream.ToArray()
        $data = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
    } catch {
        Write-Host "요청 실패: $($_.Exception.Message)" -ForegroundColor Red
        continue
    }

    if ($data.errorMessage -and $data.errorMessage.code -ne 'INFO-000') {
        Write-Host "API 응답: $($data.errorMessage.code) $($data.errorMessage.message)" -ForegroundColor Yellow
    }
    if (-not $data.realtimeArrivalList) {
        Write-Host '도착 정보 없음' -ForegroundColor Yellow
        if ($data.code) { Write-Host "API 응답: $($data.code) $($data.message)" -ForegroundColor Yellow }
        continue
    }

    $data.realtimeArrivalList |
        Select-Object @{n='노선';e={$_.subwayId}},
                      @{n='상하행';e={$_.updnLine}},
                      @{n='방면';e={$_.trainLineNm}},
                      @{n='종류';e={$_.btrainSttus}},
                      @{n='남은초';e={$_.barvlDt}},
                      @{n='도착메시지';e={$_.arvlMsg2}},
                      @{n='현재위치';e={$_.arvlMsg3}},
                      @{n='생성시각';e={$_.recptnDt}} |
        Format-Table -AutoSize | Out-String -Width 200 | Write-Host
}

Write-Host "노선 코드: 1001=1호선, 1002=2호선, 1063=경의중앙선, 1006=6호선" -ForegroundColor DarkGray
