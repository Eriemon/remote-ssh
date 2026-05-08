@echo off
python "%~dp0..\..\remote_ssh.py" request-upload %*
exit /b %ERRORLEVEL%
