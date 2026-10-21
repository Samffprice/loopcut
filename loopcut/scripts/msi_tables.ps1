# Writes every table of an .msi, and its summary information, to a JSON file, through Windows
# Installer's own API (no extra tools). repackage_windows.py reads two of these to compare a
# repackaged .msi with the release it came from.
#     msi_tables.ps1 <file.msi> <out.json>
# Binary columns (icons, embedded files) are written as their size in bytes.
param(
  [Parameter(Mandatory)][string]$Msi,
  [Parameter(Mandatory)][string]$Out
)
$ErrorActionPreference = 'Stop'

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
$database = Call $installer 'OpenDatabase' @((Resolve-Path -LiteralPath $Msi).Path, 0)

function Query([string]$sql) {
  $view = Call $database 'OpenView' @($sql)
  Call $view 'Execute' @() | Out-Null
  $view
}

$names = [Collections.Generic.List[string]]::new()
$view = Query 'SELECT `Name` FROM `_Tables`'
while ($null -ne ($record = Call $view 'Fetch' @())) { $names.Add((Prop $record 'StringData' @(1))) }
Call $view 'Close' @() | Out-Null

$tables = [ordered]@{}
foreach ($name in ($names | Sort-Object)) {
  $view = Query "SELECT * FROM ``$name``"
  $info = Prop $view 'ColumnInfo' @(0)
  $kinds = Prop $view 'ColumnInfo' @(1)
  $count = Prop $info 'FieldCount' @()
  $columns = [object[]]@(1..$count | ForEach-Object { Prop $info 'StringData' @($_) })
  $types = [object[]]@(1..$count | ForEach-Object { Prop $kinds 'StringData' @($_) })
  $rows = [Collections.Generic.List[object]]::new()
  while ($null -ne ($record = Call $view 'Fetch' @())) {
    $values = [object[]]::new($count)
    for ($i = 1; $i -le $count; $i++) {
      $kind = $types[$i - 1].Substring(0, 1).ToLowerInvariant()
      if (Prop $record 'IsNull' @($i)) { $values[$i - 1] = $null }
      elseif ($kind -eq 'v') { $values[$i - 1] = "<$(Prop $record 'DataSize' @($i)) bytes>" }
      elseif ($kind -in 'i', 'j') { $values[$i - 1] = Prop $record 'IntegerData' @($i) }
      else { $values[$i - 1] = Prop $record 'StringData' @($i) }
    }
    $rows.Add($values)
  }
  Call $view 'Close' @() | Out-Null
  $tables[$name] = [ordered]@{ columns = $columns; types = $types; rows = $rows }
}

$summary = [ordered]@{}
$info = Prop $database 'SummaryInformation' @(0)
foreach ($id in 1..19) {
  $value = Prop $info 'Property' @($id)
  if ($null -ne $value) {
    $summary["$id"] = if ($value -is [datetime]) { $value.ToString('s') } else { "$value" }
  }
}

$document = [ordered]@{ file = (Split-Path -Leaf $Msi); summary = $summary; tables = $tables }
ConvertTo-Json -InputObject $document -Depth 8 -Compress | Set-Content -LiteralPath $Out -Encoding utf8
Write-Host "$Msi`: $($names.Count) tables, $($tables['File'].rows.Count) files -> $Out"
