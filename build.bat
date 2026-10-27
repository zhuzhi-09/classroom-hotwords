@echo off
rem Build dist\ClassroomWords\ClassroomWords.exe (needs Python 3.12 + pip install -r requirements.txt pyinstaller)
cd /d "%~dp0"
py -3.12 -m PyInstaller --noconfirm --windowed --name ClassroomWords --icon icon.ico --add-data "web;web" --collect-all vosk --collect-data jieba --hidden-import pystray._win32 run.pyw
