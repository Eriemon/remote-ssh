$Tool = Join-Path $PSScriptRoot "..\..\remote_ssh.py"
python $Tool file-download @args
exit $LASTEXITCODE
