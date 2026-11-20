<#
One-time install for Research Radar on Windows.

Creates the Python environment, installs the app, creates config/config.toml, and adds a
"Research Radar" shortcut to the desktop and the Start menu. Everything after this happens in
the control panel the shortcut opens.

Run from the repository folder:
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#>
param(
    [switch]$NoShortcut,
    [switch]$NoLaunch
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
$hasUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)

Write-Host "Research Radar install in $root"

if (-not (Test-Path $py)) {
    Write-Host "Creating the Python environment (.venv)..."
    if ($hasUv) {
        uv venv --python 3.12 .venv
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        py -3.12 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        python -m venv .venv
    } else {
        throw "Python 3.12 is needed. Install it with: winget install Python.Python.3.12  (or install uv), then run this script again."
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }
}

Write-Host "Installing Research Radar and its dependencies..."
if ($hasUv) {
    uv pip install --python $py -e .
} else {
    & $py -m pip install --upgrade pip --quiet
    & $py -m pip install -e .
}
if ($LASTEXITCODE -ne 0) { throw "Dependency install failed." }

$config = Join-Path $root "config\config.toml"
if (-not (Test-Path $config)) {
    Copy-Item (Join-Path $root "config\config.example.toml") $config
    Write-Host "Created config\config.toml"
}

$radar = Join-Path $root ".venv\Scripts\radar.exe"
if (-not $NoShortcut) {
    $shell = New-Object -ComObject WScript.Shell
    $folders = @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))
    foreach ($folder in $folders) {
        $link = $shell.CreateShortcut((Join-Path $folder "Research Radar.lnk"))
        $link.TargetPath = $radar
        $link.Arguments = "ui"
        $link.WorkingDirectory = $root
        $link.WindowStyle = 7  # start minimized; closing that window stops the panel
        $link.IconLocation = "$env:SystemRoot\System32\shell32.dll,22"
        $link.Description = "Open the Research Radar control panel"
        $link.Save()
    }
    Write-Host "Added 'Research Radar' to the desktop and the Start menu."
}

Write-Host ""
Write-Host "Done. The control panel opens in your browser; its Setup tab walks you through the rest."
if (-not $NoLaunch) {
    Start-Process -FilePath $radar -ArgumentList "ui" -WorkingDirectory $root -WindowStyle Minimized
}
