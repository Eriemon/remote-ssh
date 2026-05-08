@echo off
python "%~dp0..\..\remote_ssh.py" request-delete %*
exit /b %ERRORLEVEL%
