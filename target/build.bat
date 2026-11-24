@echo off
call C:\qnx800\qnxsdp-env.bat
cd /d "%~dp0"
make clean
make all
