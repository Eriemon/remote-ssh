@echo off
python "%~dp0..\..\remote_ssh.py" file-download %*
exit /b %ERRORLEVEL%
