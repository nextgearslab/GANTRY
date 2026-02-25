$ErrorActionPreference = "SilentlyContinue"
$ProgressPreference = "SilentlyContinue"

# --- 1. CONFIG & WORKDIR ---
$WorkDir = $PSScriptRoot
$EnvFile = Join-Path $WorkDir ".env"
$PythonExe = "python"
$ScriptPath = Join-Path $WorkDir "gantry.py"
$LogDir = Join-Path $WorkDir "logs"
$LogFile = Join-Path $LogDir "gantry_watchdog.log"

# --- 2. .ENV PARSER (Synced with Python) ---
$Port = 8787 
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and !$line.StartsWith("#") -and $line -contains "=") {
            $parts = $line.Split("=", 2)
            $key = $parts[0].Trim(); $val = $parts[1].Trim()
            if ($key -eq "GANTRY_PORT" -and $val -ne "") { $Port = [int]$val }
        }
    }
}

# --- 3. FUNCTIONS ---
function Log($msg, $color = "Cyan") {
  $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
  $formattedMsg = "[$ts] $msg"
  if (!(Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
  Add-Content -Path $LogFile -Value $formattedMsg
  Write-Host "  *  " -NoNewline -ForegroundColor Gray
  Write-Host $msg -ForegroundColor $color
}

function Test-GantryPort($port) {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -Method Get -TimeoutSec 2
        return ($response.ok -eq $true)
    } catch {
        return $false
    }
}

# --- 4. EXECUTION ---
Log "CHECKING Gantry (Port $Port)..."

if (Test-GantryPort $Port) {
  Log "OK: Service is healthy" "Green"
  exit 0
}

Log "DOWN: Initializing Gantry..." "Yellow"

$ConsoleLog = Join-Path $LogDir "gantry_console.log"
$CmdArgs = "/c `"$PythonExe `"$ScriptPath`" --no-access-log > `"$ConsoleLog`" 2>&1`""
Start-Process -FilePath "cmd.exe" -ArgumentList $CmdArgs -WorkingDirectory $WorkDir -WindowStyle Hidden

# Startup loop (Wait up to 20 seconds)
for ($i = 1; $i -le 10; $i++) {
  Start-Sleep -Seconds 2
  if (Test-GantryPort $Port) {
    Log "SUCCESS: Gantry is live on port $Port" "Green"
    exit 0
  }
  Log "Waiting for bind... ($i/10)" "DarkGray"
}

Log "ERROR: Gantry failed to bind to port $Port. Check logs\gantry_console_err.log" "Red"
exit 1