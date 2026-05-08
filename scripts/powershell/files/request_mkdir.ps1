$Tool = Join-Path $PSScriptRoot "..\..\remote_ssh.py"
python $Tool request-mkdir @args
exit $LASTEXITCODE
