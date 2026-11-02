# 카카오톡 CS 봇 상시 실행 — 봇이 죽으면 10초 뒤 다시 띄우는 무한 루프.
#   실행: powershell -NoProfile -ExecutionPolicy Bypass -File run_bot.ps1
#   로그: logs\runner.log (시작·종료·재시작 시각), logs\bot.log (봇 출력)
#   같은 PC에서 두 개가 동시에 돌면 같은 메시지에 두 번 답하므로, 이미 실행 중이면 바로 끝난다.

$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
# uv 실행 파일: UV_EXE 환경변수가 있으면 그 경로, 없으면 PATH의 uv
$Uv = if ($env:UV_EXE) { $env:UV_EXE } else { "uv" }
$LogDir = Join-Path $Root "logs"
$RunnerLog = Join-Path $LogDir "runner.log"
$BotLog = Join-Path $LogDir "bot.log"
$RestartDelaySeconds = 10

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-RunnerLog($message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Add-Content -Path $RunnerLog -Value $line -Encoding UTF8
    Write-Host $line
}

# 중복 실행 방지 (작업 스케줄러 + 수동 실행이 겹쳐도 하나만)
$mutex = New-Object System.Threading.Mutex($false, "Global\KakaoCSBotRunner")
if (-not $mutex.WaitOne(0)) {
    Write-RunnerLog "이미 실행 중인 run_bot.ps1이 있어 종료"
    exit 0
}

Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

$run = 0
try {
    while ($true) {
        $run += 1
        Write-RunnerLog "봇 시작 (${run}번째)"
        # cmd 리다이렉트로 봇 출력(UTF-8)을 그대로 이어 붙인다 (PowerShell 5.1의 >>는 UTF-16으로 바꿔 씀)
        & cmd.exe /c "`"$Uv`" run --no-sync main.py >> `"$BotLog`" 2>&1"
        $code = $LASTEXITCODE
        Write-RunnerLog "봇 종료 (종료 코드 $code) — ${RestartDelaySeconds}초 뒤 재시작"
        Start-Sleep -Seconds $RestartDelaySeconds
    }
}
finally {
    Write-RunnerLog "run_bot.ps1 종료"
    $mutex.ReleaseMutex()
}
