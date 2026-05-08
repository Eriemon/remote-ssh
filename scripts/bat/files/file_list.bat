@echo off
python "%~dp0..\..\remote_ssh.py" file-list %*
exit /b %ERRORLEVEL%
