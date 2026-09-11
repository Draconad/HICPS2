# Waits for the GitHub Actions builds of the commit that was just pushed,
# then downloads the iPhone .ipa and the Windows .exe into build-out\
#
# Run on its own too: if a push worked but the download didn't, run
#   powershell -ExecutionPolicy Bypass -File wait-for-builds.ps1
# again without re-pushing.
#
# Uses GitHub CLI (signed in by github-auth.bat) so no token is stored.
# Keep this file ASCII-only: Windows PowerShell 5.1 misreads UTF-8 without BOM.

param(
  [string]$Repo = ""
)

# "Continue", not "Stop": under Stop, Windows PowerShell 5.1 can kill the
# script when a native command (like `gh auth status`) writes to stderr.
# Every native call is checked through $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Pause-Exit([int]$code) {
  Write-Host ""
  Read-Host "Press Enter to close"
  exit $code
}

function Fail($message) {
  Write-Host ""
  Write-Host $message -ForegroundColor Red
  Pause-Exit 1
}

trap {
  Write-Host ""
  Write-Host "This script hit an unexpected error:" -ForegroundColor Red
  Write-Host "  $_" -ForegroundColor Red
  Write-Host ""
  Write-Host "The push itself already happened - the builds are running at"
  Write-Host "https://github.com/$Repo/actions and the files can be downloaded there."
  Pause-Exit 1
}

if (-not $Repo) {
  $repoFile = Join-Path $here "github-repo.txt"
  if (Test-Path $repoFile) { $Repo = (Get-Content $repoFile -TotalCount 1).Trim() }
}
if (-not $Repo) { Fail "Don't know which repository to watch. Put owner/name in github-repo.txt." }

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
  Write-Host "The push worked - only the automatic download needs GitHub CLI." -ForegroundColor Yellow
  Write-Host "Run github-auth.bat once, or grab the files from https://github.com/$Repo/actions"
  Pause-Exit 0
}
& gh auth status *> $null
if ($LASTEXITCODE -ne 0) { Fail "GitHub CLI isn't signed in yet. Run github-auth.bat, then this again." }

# ---- versions, for the file names --------------------------------------------
$iosBuild = ""
$yml = Join-Path $here "ios\project.yml"
if (Test-Path $yml) {
  $m = Select-String -Path $yml -Pattern 'CURRENT_PROJECT_VERSION:\s*"?(\d+)"?' | Select-Object -First 1
  if ($m) { $iosBuild = $m.Matches[0].Groups[1].Value }
}
$agentVersion = ""
$cfg = Join-Path $here "agent\hanwha_agent\config.py"
if (Test-Path $cfg) {
  $m = Select-String -Path $cfg -Pattern 'VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
  if ($m) { $agentVersion = $m.Matches[0].Groups[1].Value }
}

# What to wait for. The server image goes to ghcr.io, nothing to download.
$targets = @(
  @{ Name = "iPhone app";    Workflow = "ios.yml";    Artifact = "HanwhaMonitor-ipa"; File = "HanwhaMonitor.ipa"; Out = "HanwhaMonitor-b$iosBuild.ipa" },
  @{ Name = "Windows app";   Workflow = "agent.yml";  Artifact = "HanwhaMonitor-exe"; File = "HanwhaMonitor.exe"; Out = "HanwhaMonitor-$agentVersion.exe" },
  @{ Name = "Server image";  Workflow = "server.yml"; Artifact = $null;               File = $null;               Out = $null }
)

$sha = (git -C $here rev-parse HEAD).Trim()
Write-Host "Waiting for the builds of commit $($sha.Substring(0,7))..."
Write-Host "(The iPhone build takes 5-10 minutes.)"
Write-Host ""

function Get-Runs($workflow, $extra) {
  $args2 = @("run", "list", "--repo", $Repo, "--workflow", $workflow, "--limit", "20",
             "--json", "databaseId,headSha,status,conclusion") + $extra
  $json = (& gh @args2 2>$null | Out-String)
  if (-not $json.Trim()) { return }
  # Assign first, then return: in PowerShell 5.1 ConvertFrom-Json emits a JSON
  # array as ONE object; returning the variable enumerates it properly.
  $runs = ConvertFrom-Json $json
  return $runs
}

# ---- find the run of THIS commit for each workflow ----------------------------
# Workflows only run when files in their folder changed, so a workflow with no
# run for this commit after ~2 minutes simply wasn't needed this time.
$deadline = (Get-Date).AddMinutes(2)
foreach ($t in $targets) { $t.RunId = $null }
while ((Get-Date) -lt $deadline) {
  foreach ($t in $targets) {
    if ($t.RunId) { continue }
    $match = Get-Runs $t.Workflow @() | Where-Object { $_.headSha -eq $sha } | Select-Object -First 1
    if ($match) {
      $t.RunId = $match.databaseId
      Write-Host ("  {0,-13} build #{1} started" -f $t.Name, $t.RunId)
    }
  }
  if (@($targets | Where-Object { -not $_.RunId }).Count -eq 0) { break }
  Start-Sleep -Seconds 6
}
foreach ($t in $targets) {
  if (-not $t.RunId) { Write-Host ("  {0,-13} no changes this time - not rebuilt" -f $t.Name) -ForegroundColor DarkGray }
}
Write-Host ""

# ---- wait for them all to finish ----------------------------------------------
$active = @($targets | Where-Object { $_.RunId })
while ($true) {
  $pending = 0
  $line = ""
  foreach ($t in $active) {
    $json = (& gh run view $t.RunId --repo $Repo --json status,conclusion 2>$null | Out-String)
    if ($json.Trim()) {
      $r = ConvertFrom-Json $json
      $t.Status = $r.status
      $t.Conclusion = $r.conclusion
    }
    if ($t.Status -ne "completed") { $pending++ ; $state = $t.Status } else { $state = $t.Conclusion }
    $line += ("{0}: {1}   " -f $t.Name, $state)
  }
  Write-Host ("`r[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $line) -NoNewline
  if ($pending -eq 0) { break }
  Start-Sleep -Seconds 15
}
Write-Host ""
Write-Host ""

# ---- download results (or the error log) --------------------------------------
$outDir = Join-Path $here "build-out"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$anyFailed = $false
$saved = @()

foreach ($t in $targets) {
  $runId = $t.RunId
  if ($runId -and $t.Conclusion -ne "success") {
    $anyFailed = $true
    $log = Join-Path $outDir ("{0}-build-failed.log" -f ($t.Workflow -replace '\.yml$',''))
    & gh run view $runId --repo $Repo --log-failed 2>$null | Out-File -Encoding utf8 $log
    Write-Host ("{0} build FAILED. Log saved: {1}" -f $t.Name, $log) -ForegroundColor Red
    Write-Host "  Send that file over - it's usually a quick fix."
    $saved += $log
    continue
  }
  if (-not $t.Artifact) {
    if ($runId) { Write-Host ("{0}: built and published to ghcr.io" -f $t.Name) -ForegroundColor Green }
    continue
  }
  if (-not $runId) {
    # Not rebuilt: fetch the most recent successful build instead, so build-out always has both files.
    $last = Get-Runs $t.Workflow @("--status", "success") | Select-Object -First 1
    if (-not $last) { Write-Host ("{0}: no successful build yet" -f $t.Name) -ForegroundColor Yellow; continue }
    $runId = $last.databaseId
    Write-Host ("{0}: using the last good build #{1}" -f $t.Name, $runId)
  }
  $tmp = Join-Path $outDir ("_dl_" + $t.Artifact)
  if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
  if ($t.Workflow -eq "ios.yml") {
    # Paid-account builds go straight to TestFlight instead of producing an .ipa
    $tf = Join-Path $outDir "_dl_testflight"
    if (Test-Path $tf) { Remove-Item -Recurse -Force $tf }
    & gh run download $runId --repo $Repo --name "HanwhaMonitor-testflight" --dir $tf 2>$null
    if ($LASTEXITCODE -eq 0 -and (Test-Path (Join-Path $tf "README.txt"))) {
      Write-Host ("{0}: {1}" -f $t.Name, (Get-Content (Join-Path $tf "README.txt") -Raw).Trim()) -ForegroundColor Green
      Remove-Item -Recurse -Force $tf
      continue
    }
  }
  & gh run download $runId --repo $Repo --name $t.Artifact --dir $tmp
  $src = Join-Path $tmp $t.File
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $src)) {
    Write-Host ("{0}: download failed - get it from https://github.com/{1}/actions/runs/{2}" -f $t.Name, $Repo, $runId) -ForegroundColor Yellow
    continue
  }
  $dest = Join-Path $outDir $t.Out
  Move-Item -Force $src $dest
  Remove-Item -Recurse -Force $tmp
  Write-Host ("{0}: {1}" -f $t.Name, $dest) -ForegroundColor Green
  $saved += $dest
}

Write-Host ""
if ($saved.Count -gt 0) {
  Start-Process explorer.exe -ArgumentList "/select,`"$($saved[0])`""
}
if ($anyFailed) {
  Pause-Exit 1
}
Write-Host "Done. iPhone: update from TestFlight (or install the .ipa with iLoader); copy the .exe to the machine PC" -ForegroundColor Green
Write-Host "(next to Fwlib32.dll and fwlibe1.dll)."
Pause-Exit 0
