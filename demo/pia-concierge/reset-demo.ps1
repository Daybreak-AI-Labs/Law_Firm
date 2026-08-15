# reset-demo.ps1 - one-click pitch reset for the PIA Concierge demo.
#
# Stops anything already running, wipes the demo workspace, relaunches with
# the full environment, waits for both servers, and prints a go/no-go
# checklist. Run it before every pitch:
#
#   cd C:\Users\Cstep\Lightwork\demo\pia-concierge
#   powershell -ExecutionPolicy Bypass -File .\reset-demo.ps1
#
# The relaunch reseeds ~14 months of history (1-2 minutes) - the script waits
# and tells you when the demo is ready to present.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Bad($msg)  { Write-Host "  [!!] $msg" -ForegroundColor Red }

# 1. Stop anything holding the demo ports (old serve.py, stray uvicorn).
Step "Stopping anything on ports 8765 / 8890 / 1025"
foreach ($port in 8765, 8890, 1025) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        try {
            Stop-Process -Id $c.OwningProcess -Force -ErrorAction Stop
            Ok "stopped PID $($c.OwningProcess) on port $port"
        } catch { Bad "could not stop PID $($c.OwningProcess) on port $port - close it manually" }
    }
}
Start-Sleep -Seconds 1

# 2. Fresh workspace: the launcher reseeds automatically on boot.
Step "Wiping the demo workspace (.demo-home)"
if (Test-Path .\.demo-home) {
    Remove-Item -Recurse -Force .\.demo-home
}
if (Test-Path .\.demo-home) {
    Bad ".demo-home is still present (something still has it open) - close Explorer/terminals in that folder and rerun"
    exit 1
}
Ok "workspace clear"

# 3. Environment (per-process; nothing leaks into your profile).
Step "Setting the demo environment"
$env:MAVERICK_HOME                = Join-Path $here ".demo-home"
$env:PIA_BASE_URL                 = "http://127.0.0.1:8890"
$env:LIGHTWORK_DASHBOARD_URL      = "http://127.0.0.1:8765"
$env:ONETRUST_HOSTNAME            = "http://127.0.0.1:8890/ot-api"
$env:ONETRUST_TOKEN               = "demo-bearer-token"
$env:PIA_SEED_TENANT              = "1"
$env:MAVERICK_SHIELD_PROFILE      = "strict"
$env:MAVERICK_DEFAULT_MAX_DOLLARS = "10"
$env:MAVERICK_EXTERNAL_AGENTS     = "1"
$env:EMAIL_USER                   = "privacy-office@company.com"
$env:EMAIL_APP_PASSWORD           = "demo-app-password"  # pragma: allowlist secret
$env:EMAIL_SMTP_HOST              = "127.0.0.1"
$env:EMAIL_SMTP_PORT              = "1025"
Ok "environment set (commit $(git rev-parse --short HEAD 2>$null))"

# Model provider key: NEVER stored in this repo. Set it once, user-level:
#   setx ANTHROPIC_API_KEY "sk-ant-..."     (then open a fresh terminal)
# With it, the platform copilot and live drafting run at full speed; without
# it the demo still runs - the concierge flows are deterministic by design.
if ($env:ANTHROPIC_API_KEY) {
    Ok "ANTHROPIC_API_KEY present - copilot + live drafting enabled"
} else {
    Write-Host "  [--] no ANTHROPIC_API_KEY - demo runs deterministic; set it once with: setx ANTHROPIC_API_KEY `"<your key>`"" -ForegroundColor Yellow
}

# 4. Launch. Seeding happens on boot; the window stays open running the demo.
Step "Launching serve.py (seeding takes 1-2 minutes on a fresh workspace)"
$server = Start-Process -FilePath "python" -ArgumentList "serve.py" `
    -WorkingDirectory $here -PassThru -NoNewWindow

# 5. Wait for both servers, then verify.
Step "Waiting for the demo to come up"
$deadline = (Get-Date).AddMinutes(5)
$dashUp = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8765/overview" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $dashUp = $true; break }
    } catch { }
    if ($server.HasExited) { Bad "serve.py exited early - scroll up for its error"; exit 1 }
    Start-Sleep -Seconds 3
}

Write-Host ""
Step "Go / no-go checklist"
if ($dashUp) { Ok "Lightwork dashboard  http://127.0.0.1:8765" }
else         { Bad "dashboard did not come up within 5 minutes"; exit 1 }
try {
    $w = Invoke-WebRequest -Uri "http://127.0.0.1:8890/" -UseBasicParsing -TimeoutSec 5
    Ok "External world      http://127.0.0.1:8890"
} catch { Bad "external world (8890) not answering" }
if (Test-Path .\.demo-home\.workspace-seeded) { Ok "workspace seeded (fresh history, new-style documents)" }
else { Bad "seed marker missing - check the server output above" }

Write-Host ""
Write-Host "Demo is ready. This window is running the servers - leave it open." -ForegroundColor Green
Write-Host "Ctrl+C here stops the demo." -ForegroundColor DarkGray
Wait-Process -Id $server.Id
