@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "PYTHON_CMD=python"
set "TOOL=%SCRIPT_DIR%..\..\remote_ssh.py"

%PYTHON_CMD% "%TOOL%" configure --interactive %*
exit /b %ERRORLEVEL%
