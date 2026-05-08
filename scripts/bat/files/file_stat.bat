@echo off
python "%~dp0..\..\remote_ssh.py" file-stat %*
exit /b %ERRORLEVEL%
