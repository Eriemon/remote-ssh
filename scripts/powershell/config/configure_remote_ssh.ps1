$ErrorActionPreference = "Stop"
$Tool = Join-Path $PSScriptRoot "..\..\remote_ssh.py"
python $Tool discover @args
$DiscoverRc = $LASTEXITCODE
if ($DiscoverRc -eq 0) { exit 0 }
if ($DiscoverRc -ne 3 -and $DiscoverRc -ne 4) { exit $DiscoverRc }

Write-Host ""
Write-Host "No enabled SSH server configuration was found."
$AddNow = Read-Host "Add a server entry now? [Y/n]"
if ($AddNow -match "^(n|no)$") { exit $DiscoverRc }

python $Tool add-server --interactive @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
python $Tool discover @args
exit $LASTEXITCODE
