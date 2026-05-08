$Tool = Join-Path $PSScriptRoot "..\..\remote_ssh.py"
python $Tool request-upload @args
exit $LASTEXITCODE
