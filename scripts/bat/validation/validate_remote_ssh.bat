@echo off
python "%~dp0..\..\validate_remote_ssh.py" %*
exit /b %ERRORLEVEL%
