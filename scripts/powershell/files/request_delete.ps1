$Tool = Join-Path $PSScriptRoot "..\..\remote_ssh.py"
python $Tool request-delete @args
exit $LASTEXITCODE
