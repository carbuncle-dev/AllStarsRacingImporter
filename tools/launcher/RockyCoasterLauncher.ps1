# Rocky Coaster launcher: start Sonic Racing CrossWorlds either modded and offline, or vanilla and online.
#
#   Modded (offline): the mod's paks are in UNION\Content\Paks, UE4SS loads (Win64\dwmapi.dll), and a Windows
#                     Firewall rule blocks the game's executables from the internet (no online play, no EOS,
#                     no crash reports leave the PC).
#   Vanilla (online): the mod's paks wait in UNION\Content\Paks_disabled, UE4SS is off (dwmapi.dll.off), and the
#                     firewall rule is disabled.
#
# Files are only ever moved/renamed, never deleted; nothing changes while the game runs. Changing the firewall
# rule needs administrator rights: Windows asks once per switch (UAC). The game is started through Steam.
#
#   powershell -ExecutionPolicy Bypass -File RockyCoasterLauncher.ps1            (window)
#   powershell -ExecutionPolicy Bypass -File RockyCoasterLauncher.ps1 -Install   (desktop shortcut)
#   ... -Mode Modded|Vanilla [-NoLaunch]                                         (no window)

param(
    [ValidateSet("", "Modded", "Vanilla")] [string]$Mode = "",
    [switch]$NoLaunch,
    [switch]$Install,
    [switch]$SetFirewall,          # internal: run elevated to change the firewall rule
    [ValidateSet("", "On", "Off")] [string]$Block = ""
)

$ErrorActionPreference = "Stop"
$AppId = 2486820
$Game = "C:\Program Files (x86)\Steam\steamapps\common\SonicRacingCrossWorlds"
$Paks = Join-Path $Game "UNION\Content\Paks"
$Parked = Join-Path $Game "UNION\Content\Paks_disabled"
$Win64 = Join-Path $Game "UNION\Binaries\Win64"
$Proxy = Join-Path $Win64 "dwmapi.dll"
$ProxyOff = "$Proxy.off"
$ModPattern = "zzz_RockyCoaster_*"
$RulePrefix = "Rocky Coaster mod - block CrossWorlds online"
$Programs = @(
    (Join-Path $Game "SonicRacingCrossWorlds.exe"),
    (Join-Path $Win64 "SonicRacingCrossWorldsSteam.exe"),
    (Join-Path $Win64 "crashpad_handler.exe")
)

function Set-Firewall([bool]$On) {
    # one outbound block rule per executable; created on first use, then only enabled/disabled
    foreach ($exe in $Programs) {
        $name = "$RulePrefix ($(Split-Path $exe -Leaf))"
        $rule = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
        if (-not $rule) {
            New-NetFirewallRule -DisplayName $name -Direction Outbound -Action Block -Program $exe -Profile Any -Enabled False | Out-Null
        }
        Set-NetFirewallRule -DisplayName $name -Enabled ($(if ($On) { "True" } else { "False" }))
    }
}

if ($SetFirewall) {  # elevated child
    Set-Firewall ($Block -eq "On")
    exit 0
}

function Get-FirewallState {
    $states = foreach ($exe in $Programs) {
        $rule = Get-NetFirewallRule -DisplayName "$RulePrefix ($(Split-Path $exe -Leaf))" -ErrorAction SilentlyContinue
        if ($rule -and $rule.Enabled -eq "True") { "On" } else { "Off" }
    }
    if (($states | Select-Object -Unique).Count -eq 1) { $states[0] } else { "Mixed" }
}

function Get-ModState {
    $active = @(Get-ChildItem $Paks -Filter $ModPattern -File -ErrorAction SilentlyContinue)
    $parked = @(Get-ChildItem $Parked -Filter $ModPattern -File -ErrorAction SilentlyContinue)
    $ue4ss = Test-Path $Proxy
    if ($active.Count -gt 0 -and $parked.Count -eq 0 -and $ue4ss) { return "Modded" }
    if ($active.Count -eq 0 -and -not $ue4ss) { return "Vanilla" }
    return "Mixed"
}

function Test-GameRunning {
    return [bool](Get-Process -Name "SonicRacingCrossWorlds*" -ErrorAction SilentlyContinue)
}

function Move-ModFiles([string]$From, [string]$To) {
    New-Item -ItemType Directory -Force $To | Out-Null
    foreach ($f in Get-ChildItem $From -Filter $ModPattern -File -ErrorAction SilentlyContinue) {
        $dst = Join-Path $To $f.Name
        if (Test-Path $dst) { throw "$($f.Name) exists in both $From and $To; sort it out by hand first" }
        Move-Item $f.FullName $dst
    }
}

function Switch-Mode([string]$Target) {
    if (Test-GameRunning) { throw "The game is running: close it first." }
    if ($Target -eq "Modded") {
        Move-ModFiles $Parked $Paks
        if ((Test-Path $ProxyOff) -and -not (Test-Path $Proxy)) { Rename-Item $ProxyOff "dwmapi.dll" }
        $block = "On"
    } else {
        Move-ModFiles $Paks $Parked
        if ((Test-Path $Proxy) -and -not (Test-Path $ProxyOff)) { Rename-Item $Proxy "dwmapi.dll.off" }
        $block = "Off"
    }
    if ((Get-FirewallState) -ne $block) {
        $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -SetFirewall -Block $block"
        $p = Start-Process powershell -Verb RunAs -ArgumentList $argList -Wait -PassThru -WindowStyle Hidden
        if ($p.ExitCode -ne 0 -or (Get-FirewallState) -ne $block) {
            throw "The firewall rule could not be set (administrator permission refused?). Not launching."
        }
    }
    $state = Get-ModState
    if ($state -ne $Target) { throw "Files are in a mixed state after switching ($state)." }
}

function Start-Game { Start-Process "steam://rungameid/$AppId" }

if ($Install) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "Rocky Coaster Launcher.lnk"))
    $lnk.TargetPath = "powershell.exe"
    $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`""
    $lnk.IconLocation = (Join-Path $Game "SonicRacingCrossWorlds.exe") + ",0"
    $lnk.Save()
    Write-Host "Desktop shortcut created."
    exit 0
}

if ($Mode) {
    Switch-Mode $Mode
    Write-Host "Mode: $Mode (online block: $(Get-FirewallState))"
    if (-not $NoLaunch) { Start-Game }
    exit 0
}

# ---- window ----
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = "Rocky Coaster Launcher"
$form.Size = New-Object System.Drawing.Size(430, 230)
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.StartPosition = "CenterScreen"

$status = New-Object System.Windows.Forms.Label
$status.Location = New-Object System.Drawing.Point(16, 14)
$status.Size = New-Object System.Drawing.Size(390, 44)
$form.Controls.Add($status)

function Update-Status {
    $status.Text = "Current setup: $(Get-ModState)`r`nOnline block (firewall): $(Get-FirewallState)"
}

function Add-Button([string]$Text, [int]$Y, [string]$Target) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $Text
    $b.Location = New-Object System.Drawing.Point(16, $Y)
    $b.Size = New-Object System.Drawing.Size(382, 42)
    $b.Tag = $Target
    $b.Add_Click({
        try {
            $form.UseWaitCursor = $true
            Switch-Mode $this.Tag
            Update-Status
            Start-Game
            $form.Close()
        } catch {
            [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "Rocky Coaster Launcher") | Out-Null
            Update-Status
        } finally {
            $form.UseWaitCursor = $false
        }
    })
    $form.Controls.Add($b)
}

Add-Button "Play with Rocky Coaster (offline: internet blocked)" 66 "Modded"
Add-Button "Play vanilla (online)" 116 "Vanilla"
Update-Status
[void]$form.ShowDialog()
