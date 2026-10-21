# Installs the code signing tools for sign_windows.ps1 and points it at them through GITHUB_ENV:
# SignTool and Microsoft's Artifact Signing plugin for it, both pinned, set up for account loopcutsign
# (South Central US), certificate profile loopcutrelease. Only the Azure CLI sign-in is tried.
# Used by .github/workflows/release.yml (full build and repackage).
$ErrorActionPreference = 'Stop'
$tools = Join-Path $env:GITHUB_WORKSPACE signing
nuget install Microsoft.Windows.SDK.BuildTools -Version 10.0.28000.2705 -OutputDirectory $tools -NonInteractive
if ($LASTEXITCODE) { throw "nuget install failed" }
nuget install Microsoft.ArtifactSigning.Client -Version 1.0.146 -OutputDirectory $tools -NonInteractive
if ($LASTEXITCODE) { throw "nuget install failed" }
$signtool = "$tools\Microsoft.Windows.SDK.BuildTools.10.0.28000.2705\bin\10.0.28000.0\x64\signtool.exe"
$dlib = "$tools\Microsoft.ArtifactSigning.Client.1.0.146\bin\x64\Azure.CodeSigning.Dlib.dll"
foreach ($file in $signtool, $dlib) { if (-not (Test-Path $file)) { throw "Missing $file" } }
$metadata = "$tools\metadata.json"
@{
  Endpoint = 'https://scus.codesigning.azure.net'
  CodeSigningAccountName = 'loopcutsign'
  CertificateProfileName = 'loopcutrelease'
  CorrelationId = "$env:GITHUB_REPOSITORY run $env:GITHUB_RUN_ID"
  ExcludeCredentials = @('EnvironmentCredential', 'WorkloadIdentityCredential', 'ManagedIdentityCredential',
    'SharedTokenCacheCredential', 'VisualStudioCredential', 'VisualStudioCodeCredential',
    'AzurePowerShellCredential', 'AzureDeveloperCliCredential', 'InteractiveBrowserCredential')
} | ConvertTo-Json | Set-Content -Encoding utf8 $metadata
"LOOPCUT_SIGNTOOL=$signtool", "LOOPCUT_SIGN_DLIB=$dlib", "LOOPCUT_SIGN_METADATA=$metadata",
  "LOOPCUT_SIGN_CACHE=$tools\cache" | Add-Content $env:GITHUB_ENV
