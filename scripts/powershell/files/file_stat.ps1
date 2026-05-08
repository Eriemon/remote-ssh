$Tool = Join-Path $PSScriptRoot "..\..\remote_ssh.py"
python $Tool file-stat @args
exit $LASTEXITCODE
