@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "PYTHON_CMD=python"
set "TOOL=%SCRIPT_DIR%..\..\remote_ssh.py"

%PYTHON_CMD% "%TOOL%" discover %*
set "DISCOVER_RC=%ERRORLEVEL%"
if "%DISCOVER_RC%"=="0" exit /b 0
if not "%DISCOVER_RC%"=="3" if not "%DISCOVER_RC%"=="4" exit /b %DISCOVER_RC%

echo.
echo No enabled SSH server configuration was found.
set /p "ADD_NOW=Add a server entry now? [Y/n]: "
if /I "%ADD_NOW%"=="n" exit /b %DISCOVER_RC%
if /I "%ADD_NOW%"=="no" exit /b %DISCOVER_RC%

%PYTHON_CMD% "%TOOL%" add-server --interactive %*
if errorlevel 1 exit /b %ERRORLEVEL%

echo.
%PYTHON_CMD% "%TOOL%" discover %*
exit /b %ERRORLEVEL%
