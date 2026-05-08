$Tool = Join-Path $PSScriptRoot "..\..\remote_ssh.py"
python $Tool request-command @args
exit $LASTEXITCODE
