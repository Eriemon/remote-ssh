@echo off
python "%~dp0..\..\remote_ssh.py" request-command %*
exit /b %ERRORLEVEL%
