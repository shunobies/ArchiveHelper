@echo off
REM Launch Archive Helper GUI on Windows
set APP_DIR=%~dp0..\
set PYTHON_EXE=%APP_DIR%.venv\Scripts\pythonw.exe
set GUI_SCRIPT=%APP_DIR%rip_and_encode_gui.py

if exist "%PYTHON_EXE%" (
  start "" "%PYTHON_EXE%" "%GUI_SCRIPT%"
) else (
  start "" pyw "%GUI_SCRIPT%"
)
