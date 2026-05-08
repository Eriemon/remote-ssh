$Tool = Join-Path $PSScriptRoot "..\..\remote_ssh.py"
python $Tool run-request @args
exit $LASTEXITCODE
