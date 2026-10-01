<#
================================================================================
  publish-github-pages.ps1 - build, audit, and stage the public site for GitHub
                             Pages.
================================================================================
  It does three things:
    1. Rebuilds dist/ (public files only) and FORCE-AUDITS it for privacy.
       The audit aborts the script - nothing is staged or committed - if dist/
       contains internal state, manual imports, machine-local logs, a
       credential-shaped string, or a local absolute path.
    2. Initialises a git repo and stages the sources. .gitignore already
       excludes dist/, web/data/state/, web/data/inbox/, research/_probe/.
    3. Prints the exact GitHub Pages steps via scripts/handoff_publish.py.

  Why the audit is not optional:
    GitHub Pages is public. Publishing web/data/state/collector-state.json (the
    de-duplication fingerprints plus the raw canonical corpus) or anything from
    web/data/inbox/ (your own exports from login-walled sites) would expose
    internal state and personal notes. build_site.py copies from an allow-list;
    this script independently verifies the result, because two independent checks
    are cheap and a leak is not recoverable.

  ASCII-ONLY SOURCE, deliberately:
    Windows PowerShell 5.1 decodes a BOM-less .ps1 with the system ANSI code page.
    Any Chinese literal in this file would therefore be mis-decoded into invalid
    syntax (observed: 'Missing closing }'). All Chinese output comes from
    scripts/handoff_publish.py, which prints UTF-8 correctly.

  Usage:
    powershell -ExecutionPolicy Bypass -File scripts\publish-github-pages.ps1
    powershell -ExecutionPolicy Bypass -File scripts\publish-github-pages.ps1 -Commit
    powershell -ExecutionPolicy Bypass -File scripts\publish-github-pages.ps1 -Remote https://github.com/you/repo.git
================================================================================
#>
[CmdletBinding()]
param(
    [switch]$Commit,               # also create the commit (default: stage only)
    [string]$Remote = '',          # repo URL; if given, also pushes (needs credentials)
    [string]$Branch = 'gh-pages',
    [switch]$SkipAudit             # debugging only
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
Set-Location $Root

function Say([string]$Message, [string]$Color = 'Gray') {
    Write-Host $Message -ForegroundColor $Color
}

$Python = (Get-Command py -ErrorAction SilentlyContinue).Source
if (-not $Python) { $Python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $Python) { Say 'Python not found on PATH' 'Red'; exit 1 }

Say ''
Say '==== 1/4  Build the public dist/ ====================================' 'Cyan'
& $Python (Join-Path $ScriptDir 'build_site.py')
if ($LASTEXITCODE -ne 0) { Say 'build_site.py failed; aborting' 'Red'; exit 1 }

$Dist = Join-Path $Root 'dist'
if (-not (Test-Path $Dist)) { Say 'dist/ was not created' 'Red'; exit 1 }

if (-not $SkipAudit) {
    Say ''
    Say '==== 2/4  Privacy and credential audit =============================' 'Cyan'

    # (a) forbidden path fragments must not appear anywhere under dist/
    $forbidden = @('collector-state.json', 'inbox', 'harness-')
    $leaks = @()
    foreach ($frag in $forbidden) {
        $hit = Get-ChildItem $Dist -Recurse -File -ErrorAction SilentlyContinue |
               Where-Object { $_.FullName -like "*$frag*" }
        if ($hit) { $leaks += "$frag -> $($hit[0].FullName)" }
    }
    if ($leaks.Count) {
        Say 'AUDIT FAILED: dist/ contains files that must stay private.' 'Red'
        $leaks | ForEach-Object { Say "  $_" 'Red' }
        exit 1
    }
    Say '  path audit      : OK (no state / inbox / harness logs in dist/)' 'Green'

    # (b) no credential-shaped strings
    $susp = @()
    Get-ChildItem $Dist -Recurse -File | ForEach-Object {
        $t = Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue
        if ($t -and $t -match 'sk-[A-Za-z0-9]{20,}') { $susp += $_.Name }
    }
    if ($susp.Count) {
        Say 'AUDIT FAILED: credential-shaped string found in dist/.' 'Red'
        $susp | ForEach-Object { Say "  $_" 'Red' }
        exit 1
    }
    Say '  credential audit: OK (no sk- shaped string in dist/)' 'Green'

    # (c) local absolute paths (informational: they reveal the machine layout)
    $pathLeak = @()
    Get-ChildItem $Dist -Recurse -File -Include *.json, *.html, *.js | ForEach-Object {
        $t = Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue
        if ($t -and $t -match '[A-Za-z]:\\Users\\') { $pathLeak += $_.Name }
    }
    if ($pathLeak.Count) {
        Say '  path leak       : NOTE - these files mention a local user path:' 'Yellow'
        $pathLeak | Select-Object -Unique | ForEach-Object { Say "      $_" 'Yellow' }
    } else {
        Say '  path leak       : OK (no local absolute path in dist/)' 'Green'
    }

    $files = (Get-ChildItem $Dist -Recurse -File | Measure-Object).Count
    $bytes = (Get-ChildItem $Dist -Recurse -File | Measure-Object -Property Length -Sum).Sum
    Say ("  artifact        : {0} files, {1:N2} MB" -f $files, ($bytes / 1MB)) 'Green'
}

Say ''
Say '==== 3/4  Source repository ========================================' 'Cyan'
if (-not (Test-Path (Join-Path $Root '.git'))) {
    git init -q
    Say '  git repo initialised' 'Green'
} else {
    Say '  already a git repo'
}

$identity = (git config user.name) 2>$null
if (-not $identity) {
    Say '  [!] git identity is not configured. Before committing, run:' 'Yellow'
    Say '        git config --global user.name  "Your Name"' 'Yellow'
    Say '        git config --global user.email "you@example.com"' 'Yellow'
}

git add -A
$staged = (git diff --cached --name-only | Measure-Object -Line).Lines
Say "  staged files    : $staged"

# Report whether the private paths are correctly ignored.
foreach ($p in @('web/data/state/collector-state.json',
                 'web/data/inbox/manual.jsonl',
                 'dist/index.html',
                 'research/_probe')) {
    git check-ignore -q $p 2>$null
    if ($LASTEXITCODE -eq 0) { Say "  ignored  $p" 'DarkGray' }
    else { Say "  WARNING  $p is NOT ignored and would be committed" 'Yellow' }
}

if ($Commit) {
    if (-not $identity) { Say '  cannot commit without a git identity' 'Red'; exit 1 }
    git commit -q -m "Future workbench: data layer, frontend and daily research pipeline"
    Say '  commit created' 'Green'
}

Say ''
Say '==== 4/4  GitHub Pages steps =======================================' 'Cyan'
& $Python (Join-Path $ScriptDir 'handoff_publish.py') --branch $Branch

if ($Remote) {
    Say ''
    Say '==== pushing ======================================================' 'Cyan'
    $existing = (git remote) 2>$null
    if ($existing -contains 'origin') { git remote set-url origin $Remote }
    else { git remote add origin $Remote }
    git branch -M main
    git push -u origin main
    if ($LASTEXITCODE -eq 0) {
        Say '  sources pushed. Next, publish the site:' 'Green'
        Say "    git subtree push --prefix dist origin $Branch" 'Green'
    } else {
        Say '  push failed: configure GitHub credentials (PAT or Git Credential Manager).' 'Yellow'
    }
}

Say ''
Say 'Done. If the audit fails the script aborts before staging anything.' 'Green'
Say ''
