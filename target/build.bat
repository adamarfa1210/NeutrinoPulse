@echo off
if "%QNX_SDP_ENV%"=="" set "QNX_SDP_ENV=C:\qnx800\qnxsdp-env.bat"
if not defined QNX_HOST (
  if not exist "%QNX_SDP_ENV%" (
    echo [ERROR] QNX SDP environment script not found: "%QNX_SDP_ENV%". Set QNX_SDP_ENV or activate qnxsdp-env first.
    exit /b 1
  )
  call "%QNX_SDP_ENV%"
)
cd /d "%~dp0"
make clean
make all
