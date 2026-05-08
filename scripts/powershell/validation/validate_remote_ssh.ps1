$Tool = Join-Path $PSScriptRoot "..\..\validate_remote_ssh.py"
python $Tool @args
exit $LASTEXITCODE
