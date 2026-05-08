@echo off
python "%~dp0..\..\remote_ssh.py" workspace-check %*
exit /b %ERRORLEVEL%
