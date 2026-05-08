@echo off
python "%~dp0..\..\remote_ssh.py" request-mkdir %*
exit /b %ERRORLEVEL%
