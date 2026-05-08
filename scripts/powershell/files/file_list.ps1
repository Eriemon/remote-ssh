$Tool = Join-Path $PSScriptRoot "..\..\remote_ssh.py"
python $Tool file-list @args
exit $LASTEXITCODE
