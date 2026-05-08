$Tool = Join-Path $PSScriptRoot "..\..\remote_ssh.py"
python $Tool workspace-check @args
exit $LASTEXITCODE
