# Jarvis Studio - Windows Background Daemon Script
param (
    [Parameter(Position=0)]
    [ValidateSet("start", "stop", "status", "restart")]
    [string]$Action = "status",
    [int]$Port = 8000
)

$PidFile = Join-Path $PSScriptRoot "..\data\jarvis_studio.pid"

function Get-DaemonProcess {
    if (Test-Path $PidFile) {
        $pidNum = Get-Content $PidFile -ErrorAction SilentlyContinue
        if ($pidNum) {
            $proc = Get-Process -Id $pidNum -ErrorAction SilentlyContinue
            if ($proc -and $proc.ProcessName -like "*python*") {
                return $proc
            }
        }
    }
    return $null
}

switch ($Action) {
    "start" {
        $existing = Get-DaemonProcess
        if ($existing) {
            Write-Host "[Jarvis Studio] Already running (PID: $($existing.Id))" -ForegroundColor Yellow
            return
        }
        Write-Host "[Jarvis Studio] Starting background server on port $Port..." -ForegroundColor Cyan
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = "uv"
        $startInfo.Arguments = "run jarvis-studio serve --port $Port"
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        
        $proc = [System.Diagnostics.Process]::Start($startInfo)
        $proc.Id | Out-File -FilePath $PidFile -Encoding ascii
        Write-Host "[Jarvis Studio] Started successfully! PID: $($proc.Id)" -ForegroundColor Green
        Write-Host "[Jarvis Studio] Web UI accessible at: http://localhost:$Port/app" -ForegroundColor Green
    }
    "stop" {
        $proc = Get-DaemonProcess
        if ($proc) {
            Write-Host "[Jarvis Studio] Stopping daemon PID $($proc.Id)..." -ForegroundColor Yellow
            Stop-Process -Id $proc.Id -Force
            Remove-Item $PidFile -ErrorAction SilentlyContinue
            Write-Host "[Jarvis Studio] Daemon stopped." -ForegroundColor Green
        } else {
            Write-Host "[Jarvis Studio] No running daemon found." -ForegroundColor DarkGray
        }
    }
    "status" {
        $proc = Get-DaemonProcess
        if ($proc) {
            Write-Host "[Jarvis Studio] Status: RUNNING (PID: $($proc.Id))" -ForegroundColor Green
            Write-Host "[Jarvis Studio] Web UI: http://localhost:$Port/app" -ForegroundColor Cyan
        } else {
            Write-Host "[Jarvis Studio] Status: STOPPED" -ForegroundColor DarkGray
        }
    }
    "restart" {
        & $PSCommandPath stop
        Start-Sleep -Seconds 2
        & $PSCommandPath start -Port $Port
    }
}
