# Signs Loopcut's Windows programs and libraries with Microsoft Artifact Signing:
#     sign_windows.ps1 <folder or file> [...]
# A folder means every .exe, .dll and .pyd under it. Used by .github/workflows/release.yml, through
# cpack_sign.cmake on each package's staging folder, and once more on the .msi.
#
# Files that already carry a valid signature (Microsoft's runtime DLLs) are left alone. Each signed
# file is cached under the SHA-256 of its unsigned bytes, so the .msi, which stages the same files as
# the .zip, costs no more signatures: the Basic plan allows 5,000 a month and a release takes ~200.
#
# Needs LOOPCUT_SIGNTOOL, LOOPCUT_SIGN_DLIB, LOOPCUT_SIGN_METADATA and LOOPCUT_SIGN_CACHE (set by the
# workflow's signing tools step) and an Azure CLI sign-in holding the Certificate Profile Signer role.
param([Parameter(Mandatory, ValueFromRemainingArguments)][string[]]$Paths)

$ErrorActionPreference = 'Stop'

foreach ($name in 'LOOPCUT_SIGNTOOL', 'LOOPCUT_SIGN_DLIB', 'LOOPCUT_SIGN_METADATA', 'LOOPCUT_SIGN_CACHE') {
  if (-not [Environment]::GetEnvironmentVariable($name)) { throw "$name is not set" }
}
New-Item -ItemType Directory -Force -Path $env:LOOPCUT_SIGN_CACHE | Out-Null

$files = foreach ($path in $Paths) {
  $item = Get-Item -LiteralPath $path
  if ($item.PSIsContainer) {
    Get-ChildItem -LiteralPath $item.FullName -Recurse -File |
      Where-Object { $_.Extension -in '.exe', '.dll', '.pyd' }
  } else { $item }
}
# An empty list means the wrong folder, not a package with nothing to sign.
if (-not $files) { throw "Nothing to sign in: $($Paths -join ', ')" }

$keep = 0; $reused = 0
$pending = [System.Collections.Generic.List[object]]::new()
foreach ($file in $files) {
  if ((Get-AuthenticodeSignature -LiteralPath $file.FullName).Status -eq 'Valid') { $keep++; continue }
  $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash
  $cached = Join-Path $env:LOOPCUT_SIGN_CACHE $hash
  if (Test-Path -LiteralPath $cached) {
    Copy-Item -LiteralPath $cached -Destination $file.FullName -Force
    $reused++
  } else {
    $pending.Add([pscustomobject]@{ Path = $file.FullName; Hash = $hash })
  }
}

# Batches keep the command line far below Windows' 32,767 characters. The timestamp keeps the
# signatures valid after the certificate, which lives three days, expires.
$batchSize = 50
for ($start = 0; $start -lt $pending.Count; $start += $batchSize) {
  $batch = @($pending.GetRange($start, [Math]::Min($batchSize, $pending.Count - $start)) | ForEach-Object Path)
  for ($attempt = 1; ; $attempt++) {
    & $env:LOOPCUT_SIGNTOOL sign /fd SHA256 /tr 'http://timestamp.acs.microsoft.com' /td SHA256 `
      /dlib $env:LOOPCUT_SIGN_DLIB /dmdf $env:LOOPCUT_SIGN_METADATA @batch
    if ($LASTEXITCODE -eq 0) { break }
    if ($attempt -eq 3) { throw "signtool failed $attempt times (exit $LASTEXITCODE)" }
    Write-Warning "signtool exited $LASTEXITCODE; retrying in 30 s"
    Start-Sleep -Seconds 30
  }
}

foreach ($file in $pending) {
  $signature = Get-AuthenticodeSignature -LiteralPath $file.Path
  if ($signature.Status -ne 'Valid') { throw "Not validly signed after signing ($($signature.Status)): $($file.Path)" }
  Copy-Item -LiteralPath $file.Path -Destination (Join-Path $env:LOOPCUT_SIGN_CACHE $file.Hash) -Force
}

Write-Host "Signed $($pending.Count), reused $reused from the cache, left $keep already signed."
