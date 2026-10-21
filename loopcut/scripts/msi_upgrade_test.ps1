# Installs an earlier Loopcut .msi, then a new one over it, as a user's update does, and checks that the
# new one replaced the old as a major upgrade: a product code Windows has never seen (so never the
# Change/Repair/Remove maintenance of an installed product), the old product gone, one entry in Apps,
# the shortcuts and the .blend association pointing at the install folder, and the installed files exactly
# the new release's .zip (so the swapped Python is what got installed, and removed files are gone). Then
# starts Loopcut from there.
#     msi_upgrade_test.ps1 <old.msi> <new.msi> <new .zip> <add-on version, like v0.2.7> <log folder>
# For a disposable machine (the release workflow's runner): it really installs, for all users.
param(
  [Parameter(Mandatory)][string]$Old,
  [Parameter(Mandatory)][string]$New,
  [Parameter(Mandatory)][string]$NewZip,
  [Parameter(Mandatory)][string]$Version,
  [Parameter(Mandatory)][string]$Logs
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
$Old = (Resolve-Path -LiteralPath $Old).Path
$New = (Resolve-Path -LiteralPath $New).Path

# Late-bound COM calls. Arguments are unwrapped first: a value from the pipeline is a PSObject, which COM
# rejects as a type mismatch.
function Plain([object[]]$arguments) {
  $plain = [object[]]::new($arguments.Count)
  for ($k = 0; $k -lt $arguments.Count; $k++) { $plain[$k] = $arguments[$k].PSObject.BaseObject }
  , $plain
}
function Call($object, [string]$name, [object[]]$arguments) {
  $object.GetType().InvokeMember($name, [Reflection.BindingFlags]::InvokeMethod, $null, $object, (Plain $arguments))
}
function Prop($object, [string]$name, [object[]]$arguments) {
  $object.GetType().InvokeMember($name, [Reflection.BindingFlags]::GetProperty, $null, $object, (Plain $arguments))
}
$installer = New-Object -ComObject WindowsInstaller.Installer
function MsiProperty([string]$msi, [string]$name) {
  $database = Call $installer 'OpenDatabase' @($msi, 0)
  $view = Call $database 'OpenView' @("SELECT ``Value`` FROM ``Property`` WHERE ``Property`` = '$name'")
  Call $view 'Execute' @() | Out-Null
  $record = Call $view 'Fetch' @()
  if ($null -eq $record) { throw "$msi has no $name" }
  Prop $record 'StringData' @(1)
}
function State([string]$code) { Prop $installer 'ProductState' @($code) }  # 5 installed, -1 unknown
function Related([string]$upgrade) {
  # A COM StringList: read it in place, since PowerShell would unroll it when a function returns it.
  $list = $installer.GetType().InvokeMember('RelatedProducts', [Reflection.BindingFlags]::GetProperty, $null, $installer, @($upgrade))
  $count = $list.GetType().InvokeMember('Count', [Reflection.BindingFlags]::GetProperty, $null, $list, $null)
  $codes = [Collections.Generic.List[string]]::new()
  for ($k = 0; $k -lt $count; $k++) {
    $codes.Add($list.GetType().InvokeMember('Item', [Reflection.BindingFlags]::GetProperty, $null, $list, @($k)))
  }
  , $codes.ToArray()
}
function Install([string]$msi, [string]$log) {
  $started = Get-Date
  $process = Start-Process msiexec.exe -Wait -PassThru -ArgumentList @('/i', "`"$msi`"", '/qn', '/norestart', '/l*v', "`"$log`"")
  $seconds = [int]((Get-Date) - $started).TotalSeconds
  if ($process.ExitCode -ne 0) {
    Get-Content -LiteralPath $log -Tail 40 | Write-Host
    throw "msiexec /i $(Split-Path -Leaf $msi) exited $($process.ExitCode)"
  }
  Write-Host "Installed $(Split-Path -Leaf $msi) in $seconds s"
}
$failures = [Collections.Generic.List[string]]::new()
function Expect([bool]$ok, [string]$what) {
  Write-Host "$(if ($ok) { 'ok  ' } else { 'FAIL' }) $what"
  if (-not $ok) { $failures.Add($what) }
}

$oldCode = MsiProperty $Old 'ProductCode'; $newCode = MsiProperty $New 'ProductCode'
$upgrade = MsiProperty $New 'UpgradeCode'
Write-Host "old $(Split-Path -Leaf $Old): ProductCode $oldCode, version $(MsiProperty $Old 'ProductVersion')"
Write-Host "new $(Split-Path -Leaf $New): ProductCode $newCode, version $(MsiProperty $New 'ProductVersion'), UpgradeCode $upgrade"
Expect ($upgrade -eq (MsiProperty $Old 'UpgradeCode')) 'same UpgradeCode as the old .msi'
Expect ($oldCode -ne $newCode) 'a new ProductCode'
if ($failures.Count) { throw "Not an upgrade: $($failures -join '; ')" }

Expect ((State $oldCode) -eq -1) 'the old product is not installed yet'
Install $Old (Join-Path $Logs 'install-old.log')
Expect ((State $oldCode) -eq 5) 'the old product is installed'
Expect ((State $newCode) -eq -1) 'the new product code is unknown to Windows before its install (no maintenance mode)'

Install $New (Join-Path $Logs 'install-new.log')
$log = Get-Content -LiteralPath (Join-Path $Logs 'install-new.log') -Raw
Expect ($log -match [regex]::Escape("Adding WIX_UPGRADE_DETECTED property. Its value is '$oldCode'")) 'the new .msi found the old one as an upgrade (WIX_UPGRADE_DETECTED)'
Expect ($log -match 'Doing action: RemoveExistingProducts') 'RemoveExistingProducts ran'
Expect ($log -match [regex]::Escape("UPGRADINGPRODUCTCODE={0}" -f $newCode)) 'the old product was removed by this upgrade (UPGRADINGPRODUCTCODE)'
Expect ((State $newCode) -eq 5) 'the new product is installed'
Expect ((State $oldCode) -eq -1) 'the old product is gone'
$related = Related $upgrade
Expect (($related.Count -eq 1) -and ($related[0] -eq $newCode)) "one product with the UpgradeCode ($($related -join ', '))"

$entries = @(Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
  ForEach-Object { Get-ItemProperty $_.PSPath } | Where-Object { $_.DisplayName -like 'Loopcut*' })
Expect (($entries.Count -eq 1) -and ($entries[0].PSChildName -eq $newCode)) "one Loopcut entry in Apps ($($entries.PSChildName -join ', '))"
$entries | Select-Object PSChildName, DisplayName, DisplayVersion, Publisher, InstallLocation | Format-List | Out-String | Write-Host

$programs = Join-Path $env:ProgramFiles 'Loopcut'
$folder = Get-ChildItem -LiteralPath $programs -Directory | Select-Object -First 1
Expect ($null -ne $folder -and (Test-Path -LiteralPath (Join-Path $folder.FullName 'blender.exe'))) "blender.exe in $($folder.FullName)"
$launcher = Join-Path $folder.FullName 'blender-launcher.exe'

$shell = New-Object -ComObject WScript.Shell
$desktops = "$env:PUBLIC\Desktop", [Environment]::GetFolderPath('Desktop')
$menus = "$env:ProgramData\Microsoft\Windows\Start Menu\Programs", "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
$links = @(Get-ChildItem -Recurse -Filter 'Loopcut*.lnk' -LiteralPath ($desktops + $menus) -ErrorAction SilentlyContinue)
foreach ($link in $links) {
  $target = $shell.CreateShortcut($link.FullName).TargetPath
  Write-Host "shortcut $($link.FullName) -> $target"
}
Expect (@($links | Where-Object { $_.FullName -like '*Start Menu*' }).Count -ge 1) 'a Start menu shortcut'
Expect (@($links | Where-Object { $_.FullName -notlike '*Start Menu*' }).Count -ge 1) 'a desktop shortcut'
Expect (@($links | Where-Object { $shell.CreateShortcut($_.FullName).TargetPath -ne $launcher }).Count -eq 0) "every shortcut opens $launcher"

$progId = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Classes\.blend' -ErrorAction SilentlyContinue).'(default)'
$command = if ($progId) { (Get-ItemProperty -LiteralPath "HKLM:\SOFTWARE\Classes\$progId\shell\open\command" -ErrorAction SilentlyContinue).'(default)' }
Write-Host ".blend -> $progId -> $command"
Expect ("$command" -like "*$($folder.FullName)*") '.blend files open with the installed Loopcut'

# Every installed file against the new .zip, by SHA-256 (before Loopcut runs and writes its caches).
Add-Type -AssemblyName System.IO.Compression.FileSystem
$sha = [Security.Cryptography.SHA256]::Create()
$zip = [IO.Compression.ZipFile]::OpenRead((Resolve-Path -LiteralPath $NewZip).Path)
$top = $zip.Entries[0].FullName.Split('/')[0]
$expected = @{}
foreach ($entry in $zip.Entries) {
  if ($entry.FullName.EndsWith('/')) { continue }
  $stream = $entry.Open()
  try { $expected[$entry.FullName.Substring($top.Length + 1).Replace('/', '\')] = [Convert]::ToHexString($sha.ComputeHash($stream)) }
  finally { $stream.Close() }
}
$zip.Dispose()
$root = $folder.FullName.TrimEnd('\') + '\'
$installed = @{}
foreach ($file in Get-ChildItem -LiteralPath $folder.FullName -Recurse -File) {
  $installed[$file.FullName.Substring($root.Length)] = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash
}
$missing = @($expected.Keys | Where-Object { -not $installed.ContainsKey($_) })
$extra = @($installed.Keys | Where-Object { -not $expected.ContainsKey($_) })
$different = @($expected.Keys | Where-Object { $installed.ContainsKey($_) -and $installed[$_] -ne $expected[$_] })
foreach ($list in @(@('missing', $missing), @('not in the .zip', $extra), @('different', $different))) {
  $list[1] | Select-Object -First 10 | ForEach-Object { Write-Host "  $($list[0]): $_" }
}
Expect (($missing.Count + $extra.Count + $different.Count) -eq 0) "the installed files are the new .zip's, byte for byte ($($installed.Count) files; $($missing.Count) missing, $($extra.Count) extra, $($different.Count) different)"

$smoke = Join-Path $Logs 'smoke-installed.log'
$expr = "import bpy, loopcut; assert hasattr(bpy.types, 'SpaceLoopcut'); v = 'v%d.%d.%d' % tuple(loopcut.bl_info['version']); assert v == '$Version', v; print('Loopcut installed OK', v, bpy.app.version_string)"
& (Join-Path $folder.FullName 'blender.exe') -b --factory-startup --python-exit-code 1 --python-expr $expr *> $smoke
$ok = ($LASTEXITCODE -eq 0) -and (Select-String -LiteralPath $smoke -Pattern 'Loopcut installed OK' -Quiet)
if (-not $ok) { Get-Content -LiteralPath $smoke -Tail 30 | Write-Host }
Expect $ok "the installed Loopcut starts and reports $Version"
Select-String -LiteralPath $smoke -Pattern 'Loopcut installed OK' | ForEach-Object { Write-Host $_.Line }

if ($failures.Count) { throw "Upgrade test failed: $($failures -join '; ')" }
Write-Host 'Upgrade test passed.'
