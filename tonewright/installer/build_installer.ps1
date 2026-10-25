<#
  Builds Tonewright-Setup-<version>.exe

      powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1

  What it does: stages a private copy of Python (the exact version the app is tested with), the pinned libraries,
  the program and FFmpeg under installer\build\stage; runs a smoke test with that Python; then compiles the
  installer with Inno Setup into installer\dist.

  Needs: the project's .venv (the app's tested libraries), FFmpeg on this PC (winget install Gyan.FFmpeg),
  and Inno Setup 6 (winget install JRSoftware.InnoSetup). Downloads only the Python runtime, once, into
  installer\cache, and checks it against a pinned SHA-256.
#>
[CmdletBinding()]
param(
    [string]$FfmpegDir = "",              # folder with ffmpeg.exe and ffprobe.exe; found automatically if empty
    [switch]$AllowUnpinnedPython,         # accept a Python runtime whose SHA-256 is not listed below
    [switch]$AllowUnpinnedFfmpeg          # accept an FFmpeg whose SHA-256 is not listed below
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$here = $PSScriptRoot
$root = Split-Path $here -Parent
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
$cache = Join-Path $here "cache"
$build = Join-Path $here "build"
$stage = Join-Path $build "stage"
$dist = Join-Path $here "dist"

# SHA-256 of python-<version>-embed-amd64.zip from python.org, checked when it was first downloaded
# (its python315.dll was also confirmed identical to the one in the installed 3.15.0).
$PinnedPython = @{ "3.15.0" = "A6999BCBCBE56F937159EF79C2630260ECA99F65F7860BC5CD20430324079A44" }

# SHA-256 of the FFmpeg that ships: gyan.dev 9.0.2 "full_build" (winget package Gyan.FFmpeg). gyan.dev publishes a hash for
# the archive, not for the programs inside it, so these were taken from the copy the 1.0 and 1.1 installers were built
# from; the package on the build PC, the staged copy and the installed copy all matched. To move to a newer FFmpeg,
# install it, check the build's own hash, and change both values together.
$PinnedFfmpeg = @{
    "ffmpeg.exe"  = "589E50B766D251AFDF181DD664D40BD94407E200B019989FD7468C7D118A28D0"
    "ffprobe.exe" = "02264DFA4F56BAAA31E443ECE8AA16D397F6B008200E7FD184D22B9009E75FD3"
}

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "ERROR: $text" -ForegroundColor Red; exit 1 }
function Invoke-Robocopy([string[]]$Arguments) {
    & robocopy @Arguments | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "robocopy failed ($LASTEXITCODE): $($Arguments -join ' ')" }
    $global:LASTEXITCODE = 0
}

if (-not (Test-Path $venvPy)) { Fail "The project environment is missing. Run tonewright\Tonewright.bat once to create .venv." }

# ------------------------------------------------------------------ versions
Step "Checking versions"
$env:PYTHONPATH = $root
$version = (& $venvPy -c "import tonewright; print(tonewright.__version__)").Trim()
$pyVersion = (& $venvPy -c "import sys; print('%d.%d.%d' % sys.version_info[:3])").Trim()
Write-Host "Tonewright $version, Python $pyVersion"

$lock = Get-Content (Join-Path $root "requirements.lock.txt") | Where-Object { $_ -match '^[A-Za-z]' }
$installed = & $venvPy -m pip freeze --disable-pip-version-check
foreach ($line in $lock) {
    if ($line -notmatch '^([A-Za-z0-9_.\-]+)==([^\s\\]+)') { Fail "Cannot read this line of requirements.lock.txt: $line" }
    $name, $want = $Matches[1], $Matches[2]
    $have = $installed | Where-Object { ($_ -replace '_', '-') -like "$name==*" }
    if (-not $have -or ($have -split '==')[1] -ne $want) { Fail "The environment does not match requirements.lock.txt for $name (wanted $want, found '$have')." }
}
Write-Host "Libraries match requirements.lock.txt"

# ------------------------------------------------------------------ python runtime
Step "Python runtime"
New-Item -ItemType Directory -Force $cache | Out-Null
$zipName = "python-$pyVersion-embed-amd64.zip"
$zip = Join-Path $cache $zipName
if (-not (Test-Path $zip)) {
    Write-Host "Downloading $zipName from python.org"
    Invoke-WebRequest "https://www.python.org/ftp/python/$pyVersion/$zipName" -OutFile $zip -UseBasicParsing
}
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash
if ($PinnedPython.ContainsKey($pyVersion)) {
    if ($hash -ne $PinnedPython[$pyVersion]) { Remove-Item $zip -Force; Fail "$zipName does not match the pinned SHA-256 (got $hash). Deleted it; run again." }
    Write-Host "SHA-256 matches the pinned value"
} elseif ($AllowUnpinnedPython) {
    Write-Warning "Python $pyVersion has no pinned hash. Its SHA-256 is $hash; add it to build_installer.ps1."
} else {
    Fail "No pinned SHA-256 for Python $pyVersion (the download hashed to $hash). Verify it, add it to `$PinnedPython, or pass -AllowUnpinnedPython."
}

# ------------------------------------------------------------------ stage
Step "Staging"
if ((Split-Path $stage -Leaf) -ne "stage" -or -not $stage.StartsWith($here)) { Fail "Unexpected stage path $stage" }
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force $stage | Out-Null
Expand-Archive $zip (Join-Path $stage "python")

# only the interpreter's own library zip, the program and the packages are importable (no site, no PYTHONPATH)
$pth = Get-ChildItem (Join-Path $stage "python") -Filter "python3*._pth" | Select-Object -First 1
$zipStd = (Get-ChildItem (Join-Path $stage "python") -Filter "python3*.zip" | Select-Object -First 1).Name
Set-Content $pth.FullName -Value @($zipStd, ".", "..\site-packages", "..\app") -Encoding ascii

Write-Host "Libraries"
$sp = Join-Path $root ".venv\Lib\site-packages"
Invoke-Robocopy @($sp, (Join-Path $stage "site-packages"), "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
    "/XD", "pip", "pip-*", "_pytest", "pytest", "pytest-*", "pluggy", "pluggy-*", "iniconfig", "iniconfig-*", "pygments", "pygments-*",
    "colorama", "colorama-*", "packaging", "packaging-*", "__pycache__", "tests", "test", "_virtualenv*",
    "/XF", "*.pyi", "py.py", "*.pth")

Write-Host "Trimming Qt to what the program uses (Core, Gui, Widgets, Svg)"
$qt = Join-Path $stage "site-packages\PySide6"
$keepPyd = @("QtCore", "QtGui", "QtWidgets", "QtSvg")
$keepDll = @("Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll", "Qt6Svg.dll")
Get-ChildItem $qt -Filter *.pyd | Where-Object { $keepPyd -notcontains $_.BaseName } | Remove-Item -Force
Get-ChildItem $qt -Filter "Qt6*.dll" | Where-Object { $keepDll -notcontains $_.Name } | Remove-Item -Force
foreach ($f in "opengl32sw.dll", "d3dcompiler_47.dll", "pyside6qml.abi3.dll", "py.typed") { Remove-Item (Join-Path $qt $f) -Force -ErrorAction SilentlyContinue }
Get-ChildItem $qt -File | Where-Object { $_.Extension -in ".exe", ".lib", ".pyi" } | Remove-Item -Force
foreach ($d in "doc", "glue", "include", "lib", "metatypes", "qml", "resources", "scripts", "support", "typesystems", "__pycache__") {
    Remove-Item (Join-Path $qt $d) -Recurse -Force -ErrorAction SilentlyContinue
}
Get-ChildItem (Join-Path $qt "translations") -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -notin "qtbase_uk.qm", "qt_uk.qm" } | Remove-Item -Force
$plugins = Join-Path $qt "plugins"
$keepPlugins = @{ "platforms" = @("qwindows.dll"); "styles" = @("*"); "imageformats" = @("qjpeg.dll", "qgif.dll"); "iconengines" = @("qsvgicon.dll") }
Get-ChildItem $plugins -Directory | ForEach-Object {
    if (-not $keepPlugins.ContainsKey($_.Name)) { Remove-Item $_.FullName -Recurse -Force; return }
    $allow = $keepPlugins[$_.Name]
    if ($allow -notcontains "*") { Get-ChildItem $_.FullName -File | Where-Object { $allow -notcontains $_.Name } | Remove-Item -Force }
}

Write-Host "Program"
$app = Join-Path $stage "app"
Invoke-Robocopy @((Join-Path $root "tonewright"), (Join-Path $app "tonewright"), "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/XD", "__pycache__", "/XF", "*.pyc")
Remove-Item (Join-Path $app "tonewright\assets\__pycache__") -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "FFmpeg"
if (-not $FfmpegDir) {
    $candidates = @()
    $winget = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path $winget) { $candidates += Get-ChildItem $winget -Directory -Filter "*FFmpeg*" | ForEach-Object { Get-ChildItem $_.FullName -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue } }
    $onPath = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += Get-Item $onPath.Source }
    $pick = $candidates | Where-Object { Test-Path (Join-Path $_.DirectoryName "ffprobe.exe") } | Select-Object -First 1
    if (-not $pick) { Fail "FFmpeg was not found. Install it (winget install Gyan.FFmpeg) or pass -FfmpegDir." }
    $FfmpegDir = $pick.DirectoryName
}
foreach ($exe in "ffmpeg.exe", "ffprobe.exe") { if (-not (Test-Path (Join-Path $FfmpegDir $exe))) { Fail "$exe is missing from $FfmpegDir" } }
foreach ($exe in "ffmpeg.exe", "ffprobe.exe") {
    $got = (Get-FileHash (Join-Path $FfmpegDir $exe) -Algorithm SHA256).Hash
    if ($got -eq $PinnedFfmpeg[$exe]) { Write-Host "$exe matches the pinned SHA-256" }
    elseif ($AllowUnpinnedFfmpeg) { Write-Warning "$exe is not the pinned build. Its SHA-256 is $got; update `$PinnedFfmpeg in build_installer.ps1." }
    else { Fail "$exe in $FfmpegDir does not match the pinned SHA-256 (got $got). Install the pinned FFmpeg, or check this build and update `$PinnedFfmpeg, or pass -AllowUnpinnedFfmpeg." }
}
$bin = Join-Path $app "bin"
New-Item -ItemType Directory -Force $bin | Out-Null
Copy-Item (Join-Path $FfmpegDir "ffmpeg.exe"), (Join-Path $FfmpegDir "ffprobe.exe") $bin
$ffVersion = ((& (Join-Path $bin "ffmpeg.exe") -version)[0])
Write-Host $ffVersion

Write-Host "Licences and icon"
$lic = Join-Path $stage "licenses"
New-Item -ItemType Directory -Force $lic | Out-Null
Copy-Item (Join-Path $stage "python\LICENSE.txt") (Join-Path $lic "Python-LICENSE.txt")
$ffRoot = Split-Path $FfmpegDir -Parent
foreach ($pair in @(@("LICENSE", "FFmpeg-LICENSE.txt"), @("README.txt", "FFmpeg-build-README.txt"))) {
    $src = Join-Path $ffRoot $pair[0]
    if (Test-Path $src) { Copy-Item $src (Join-Path $lic $pair[1]) }
}
Copy-Item (Join-Path $here "THIRD-PARTY-NOTICES.txt") (Join-Path $stage "THIRD-PARTY-NOTICES.txt")
Copy-Item (Join-Path $root "tonewright\assets\tonewright.ico") (Join-Path $stage "Tonewright.ico")
Copy-Item (Join-Path $root "README.md") (Join-Path $stage "README.txt")

Write-Host "Pre-compiling (so the first start is quick, and works from a read-only folder)"
& $venvPy -m compileall -q -f --invalidation-mode unchecked-hash (Join-Path $stage "app") (Join-Path $stage "site-packages") | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "compileall failed" }

$mb = [math]::Round((Get-ChildItem $stage -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "Staged $mb MB"

# ------------------------------------------------------------------ test the staged copy with its own Python
Step "Smoke test (the bundled Python, no window shown)"
& (Join-Path $stage "python\python.exe") (Join-Path $here "smoke_test.py") --stage $stage
if ($LASTEXITCODE -ne 0) { Fail "The staged copy failed its smoke test; no installer was built." }

# ------------------------------------------------------------------ installer
Step "Compiling the installer"
$iscc = @((Get-Command ISCC -ErrorAction SilentlyContinue).Source,
          (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
          (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
          (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) { Fail "Inno Setup 6 was not found. Install it with: winget install JRSoftware.InnoSetup" }
New-Item -ItemType Directory -Force $dist | Out-Null
Push-Location $here
try { & $iscc "/Qp" "/DAppVersion=$version" "/DStageDir=$stage" "tonewright.iss" }
finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { Fail "Inno Setup failed" }

$setup = Join-Path $dist "Tonewright-Setup-$version.exe"
$sha = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
"$sha *$(Split-Path $setup -Leaf)" | Set-Content "$setup.sha256" -Encoding ascii
Step "Done"
Write-Host ("{0}  ({1} MB)" -f $setup, [math]::Round((Get-Item $setup).Length / 1MB, 1))
Write-Host "SHA-256: $sha"
