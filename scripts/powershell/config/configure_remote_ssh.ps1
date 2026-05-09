$ErrorActionPreference = "Stop"
$Tool = Join-Path $PSScriptRoot "..\..\remote_ssh.py"
python $Tool configure --interactive @args
exit $LASTEXITCODE
