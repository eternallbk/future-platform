<#
================================================================================
  publish-gh-pages.ps1 - publish dist/ to the gh-pages branch of origin.
================================================================================
  WHY NOT `git subtree push --prefix dist`:
    dist/ is generated and gitignored, so it is NOT a tracked path in any commit.
    `git subtree split --prefix` reads committed history only, so it fails with
    "fatal: 'dist' does not exist; use 'git subtree add'"; a following
    `git subtree add --prefix dist` then fails with "prefix 'dist' already
    exists" because dist/ is on disk. Wrong tool for a build-output directory.

  WHY THE DEPLOY REPO LIVES IN %TEMP% AND NOT INSIDE dist/:
    An earlier version put a small repo at dist/.git. That was a serious mistake.
    Once dist/.git lost its HEAD/config it stopped being a valid repository, and
    git's directory discovery then WALKED UP the tree and silently used the parent
    repository instead. Observed damage:
      * `git -C dist checkout -B gh-pages` created a gh-pages BRANCH IN THE MAIN
        REPO and switched the working copy onto it
      * four "Publish workbench site" commits landed on that branch containing
        real source files (scripts/, web/data/, README.md) instead of the site
      * the push then failed, leaving the main repo on the wrong branch
    Nothing reached GitHub (the network was down) and the parent repo was
    restored. To make that class of error impossible this script now:
      * builds a throwaway repo in the temp directory via `git init`
      * sets GIT_DIR + GIT_WORK_TREE explicitly on every git call, so git never
        searches parent directories
      * VERIFIES the resolved repo root equals the temp dir and aborts otherwise
      * never runs a git command with a bare `-C dist`

  CREDENTIALS / UNATTENDED PUSH:
    Git Credential Manager is the system credential helper and a github.com token
    is stored, so this works with no prompt. For a long-lived schedule use a
    fine-grained PAT limited to this one repository with Contents: Read and write.
    On failure the script reports it and the caller treats the data refresh as
    successful regardless.

  ASCII-ONLY SOURCE, deliberately: Windows PowerShell 5.1 decodes a BOM-less .ps1
  with the system ANSI code page, so any Chinese literal here would be mis-decoded
  into invalid syntax. Chinese output comes from scripts/handoff_ghpages.py.

  Usage:
    powershell -ExecutionPolicy Bypass -File scripts\publish-gh-pages.ps1
    powershell -ExecutionPolicy Bypass -File scripts\publish-gh-pages.ps1 -Rebuild
    powershell -ExecutionPolicy Bypass -File scripts\publish-gh-pages.ps1 -DryRun
================================================================================
#>
[CmdletBinding()]
param(
    [string]$Remote = '',
    [string]$Branch = 'gh-pages',
    [switch]$Rebuild,     # run build_site.py first
    [switch]$DryRun,      # do everything except the push
    [switch]$SkipAudit
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
Set-Location $Root

function Say([string]$Message, [string]$Color = 'Gray') { Write-Host $Message -ForegroundColor $Color }

# Python needs UTF-8 explicitly: its stdout otherwise uses the system code page and
# the Chinese hand-off text becomes mojibake.
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }

<#
  RunNative - invoke an external command without letting its stderr kill the run.

  WHY: with $ErrorActionPreference = 'Stop', Windows PowerShell 5.1 turns anything
  a native command writes to stderr into a TERMINATING error. git writes ordinary
  progress - and even "Everything up-to-date" - to stderr, so a plain `git push`
  aborted the enclosing script before its exit code was ever checked, and one
  transient network error took down the whole daily run.
#>
function RunNative {
    param([string]$Exe, [string[]]$Arguments, [hashtable]$Env = $null)
    $prev = $ErrorActionPreference
    $saved = @{}
    $ErrorActionPreference = 'Continue'
    try {
        if ($Env) {
            foreach ($k in $Env.Keys) {
                $saved[$k] = [Environment]::GetEnvironmentVariable($k)
                [Environment]::SetEnvironmentVariable($k, $Env[$k])
            }
        }
        $out = & $Exe @Arguments 2>&1 | Out-String
        $code = $LASTEXITCODE
    } finally {
        if ($Env) {
            foreach ($k in $Env.Keys) {
                [Environment]::SetEnvironmentVariable($k, $saved[$k])
            }
        }
        $ErrorActionPreference = $prev
    }
    return @{ code = $code; out = $out }
}

$Python = (Get-Command py -ErrorAction SilentlyContinue).Source
if (-not $Python) { $Python = (Get-Command python -ErrorAction SilentlyContinue).Source }

$Dist = Join-Path $Root 'dist'

Say ''
Say '==== 1/5  Build dist/ ==============================================' 'Cyan'
if ($Rebuild -or -not (Test-Path $Dist)) {
    if (-not $Python) { Say 'Python not found; cannot build' 'Red'; exit 1 }
    & $Python (Join-Path $ScriptDir 'build_site.py')
    if ($LASTEXITCODE -ne 0) { Say 'build_site.py failed' 'Red'; exit 1 }
} else {
    Say '  dist/ already exists (use -Rebuild to regenerate)' 'DarkGray'
}
if (-not (Test-Path (Join-Path $Dist 'index.html'))) { Say 'dist/index.html missing' 'Red'; exit 1 }

if (-not $SkipAudit) {
    Say ''
    Say '==== 2/5  Privacy audit before publishing ==========================' 'Cyan'
    # Filename-based checks on purpose: matching on CONTENT would false-positive,
    # because the word "inbox" legitimately appears inside sources.json etc.
    foreach ($frag in @('collector-state.json', 'harness-')) {
        $hit = Get-ChildItem $Dist -Recurse -File -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -like "*$frag*" }
        if ($hit) { Say "  AUDIT FAILED: $frag present in dist/" 'Red'; exit 1 }
    }
    if (Test-Path (Join-Path $Dist 'data\inbox')) {
        Say '  AUDIT FAILED: dist/data/inbox exists' 'Red'; exit 1
    }
    $susp = @()
    Get-ChildItem $Dist -Recurse -File | ForEach-Object {
        $t = Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue
        if ($t -and $t -match 'sk-[A-Za-z0-9]{20,}') { $susp += $_.Name }
    }
    if ($susp.Count) { Say '  AUDIT FAILED: credential-shaped string in dist/' 'Red'; exit 1 }
    $files = (Get-ChildItem $Dist -Recurse -File | Measure-Object).Count
    $bytes = (Get-ChildItem $Dist -Recurse -File | Measure-Object -Property Length -Sum).Sum
    Say ("  audit OK: {0} files, {1:N2} MB, no private paths or credentials" -f $files, ($bytes / 1MB)) 'Green'
}

# Resolve the remote before creating anything.
if ($Remote) {
    $url = $Remote
} else {
    $r = RunNative 'git' @('-C', $Root, 'remote', 'get-url', 'origin')
    $url = "$($r.out)".Trim()
    if (-not $url -or $r.code -ne 0) { Say '  no origin remote; pass -Remote <url>' 'Red'; exit 1 }
}

# ---------------------------------------------------------------------------
# Throwaway deploy repo in temp. Never inside dist/, never inside the project.
# ---------------------------------------------------------------------------
Say ''
Say '==== 3/5  Stage dist/ into a throwaway deploy repo =================' 'Cyan'
Say "  remote: $url" 'DarkGray'

$Deploy = Join-Path ([System.IO.Path]::GetTempPath()) ("future-deploy-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $Deploy | Out-Null
$envForGit = @{ GIT_DIR = (Join-Path $Deploy '.git'); GIT_WORK_TREE = $Deploy }

$null = RunNative 'git' @('init', '-q', '--initial-branch', $Branch) -Env $envForGit
if ($LASTEXITCODE -ne 0) { $null = RunNative 'git' @('init', '-q') -Env $envForGit }

# HARD GUARD: prove the resolved repository is the temp dir, not the project.
$top = "$((RunNative 'git' @('rev-parse', '--show-toplevel') -Env $envForGit).out)".Trim()
$realDeploy = (Resolve-Path $Deploy).Path
if (-not $top -or ($top -replace '/', '\').TrimEnd('\') -ne $realDeploy.TrimEnd('\')) {
    Say "  ABORT: deploy repo resolved to '$top' but expected '$realDeploy'" 'Red'
    Say '  Refusing to run git against the wrong repository.' 'Red'
    Remove-Item -Recurse -Force $Deploy -ErrorAction SilentlyContinue
    exit 1
}
Say "  deploy repo: $realDeploy" 'DarkGray'

$null = RunNative 'git' @('remote', 'add', 'origin', $url) -Env $envForGit
$null = RunNative 'git' @('config', 'user.name', 'future-workbench-bot') -Env $envForGit
$null = RunNative 'git' @('config', 'user.email', 'future-workbench-bot@users.noreply.github.com') -Env $envForGit
$null = RunNative 'git' @('config', 'core.autocrlf', 'false') -Env $envForGit
$null = RunNative 'git' @('config', 'core.safecrlf', 'false') -Env $envForGit

$copied = 0
Get-ChildItem $Dist -Recurse -File -Force | ForEach-Object {
    $rel = $_.FullName.Substring($Dist.Length).TrimStart('\', '/')
    $target = Join-Path $Deploy $rel
    $dir = Split-Path -Parent $target
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Copy-Item $_.FullName $target -Force
    $copied++
}
Say "  copied $copied file(s) into the deploy repo" 'Green'

$null = RunNative 'git' @('add', '-A') -Env $envForGit
$stagedCount = ("$((RunNative 'git' @('diff', '--cached', '--name-only') -Env $envForGit).out)" -split "`n" |
                Where-Object { $_.Trim() }).Count
Say "  staged $stagedCount file(s)" 'Green'
$stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm')
$null = RunNative 'git' @('commit', '-q', '-m', "Publish workbench site ($stamp)") -Env $envForGit

if ($DryRun) {
    Say ''
    Say '  -DryRun: skipping the push.' 'Yellow'
    Remove-Item -Recurse -Force $Deploy -ErrorAction SilentlyContinue
    Say ''
    exit 0
}

# Reuse detection by TREE comparison, not commit id: a rebuild produces a new
# commit every time, so comparing ids would always look "different" and every run
# would push a pointless commit.
$remoteHead = ''
$lsr = RunNative 'git' @('ls-remote', 'origin', $Branch) -Env $envForGit
if ("$($lsr.out)" -match '([0-9a-f]{40})') { $remoteHead = $Matches[1] }
if ($remoteHead) {
    $null = RunNative 'git' @('fetch', '-q', '--depth', '1', 'origin', $Branch) -Env $envForGit
    if ($LASTEXITCODE -eq 0) {
        $remoteTree = "$((RunNative 'git' @('rev-parse', 'FETCH_HEAD^{tree}') -Env $envForGit).out)".Trim()
        $localTree = "$((RunNative 'git' @('rev-parse', 'HEAD^{tree}') -Env $envForGit).out)".Trim()
        if ($remoteTree -and $remoteTree -eq $localTree) {
            Say ''
            Say '  gh-pages already matches this build exactly; nothing to publish' 'Green'
            Remove-Item -Recurse -Force $Deploy -ErrorAction SilentlyContinue
            if ($Python) { & $Python (Join-Path $ScriptDir 'handoff_ghpages.py') --done }
            Say ''
            exit 0
        }
    }
}

Say ''
Say '==== 5/5  Push to gh-pages ========================================' 'Cyan'
$push = RunNative 'git' @('push', '-f', 'origin', "HEAD:refs/heads/$Branch") -Env $envForGit
"$($push.out)" -split "`n" | Where-Object { $_.Trim() } | ForEach-Object { Say "  $($_.Trim())" 'DarkGray' }
if ($push.code -ne 0) {
    Say ''
    Say '  push failed. Likely cause: no credential helper / token, or the network.' 'Yellow'
    Remove-Item -Recurse -Force $Deploy -ErrorAction SilentlyContinue
    if ($Python) { & $Python (Join-Path $ScriptDir 'handoff_ghpages.py') --trouble }
    exit 1
}

Remove-Item -Recurse -Force $Deploy -ErrorAction SilentlyContinue
Say ''
Say '  published. GitHub needs ~1 minute to build the site.' 'Green'
if ($Python) { & $Python (Join-Path $ScriptDir 'handoff_ghpages.py') --done }
Say ''
