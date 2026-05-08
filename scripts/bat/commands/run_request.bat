@echo off
python "%~dp0..\..\remote_ssh.py" run-request %*
exit /b %ERRORLEVEL%
