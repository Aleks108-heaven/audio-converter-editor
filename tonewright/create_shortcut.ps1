# Creates a Tonewright shortcut on the Desktop and in the Start menu.
# Run:  powershell -ExecutionPolicy Bypass -File create_shortcut.ps1
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    Write-Host "Run Tonewright.bat once first so the Python environment exists." -ForegroundColor Yellow
    exit 1
}
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath("Desktop"), (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"))) {
    $lnk = $shell.CreateShortcut((Join-Path $folder "Tonewright.lnk"))
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "-m tonewright"
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = (Join-Path $root "tonewright\assets\tonewright.ico")
    $lnk.Description = "Tonewright - audio converter and editor"
    $lnk.Save()
    Write-Host "Created $($lnk.FullName)"
}
