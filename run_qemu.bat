@echo off
REM NeutrinoPulse - QNX 8.0 x86_64 QEMU Launcher (2 Cores + SLIRP Networking)
REM Host SSH Port: 2222 -> Target 22 | Host qconn Port: 8000 -> Target 8000
REM Target -> Host UDP Gateway: 10.0.2.2

if "%QEMU_EXE%"=="" set "QEMU_EXE=C:\Program Files\qemu\qemu-system-x86_64.exe"
if "%QNX_QEMU_DISK%"=="" set "QNX_QEMU_DISK=C:\QNX\qemu_target\output\disk-qemu"

if not exist "%QEMU_EXE%" (
  echo [ERROR] QEMU binary not found: "%QEMU_EXE%". Set QEMU_EXE env var.
  exit /b 1
)
if not exist "%QNX_QEMU_DISK%" (
  echo [ERROR] QNX disk image not found: "%QNX_QEMU_DISK%". Set QNX_QEMU_DISK env var.
  exit /b 1
)

"%QEMU_EXE%" ^
  -m 1G ^
  -smp 2 ^
  -cpu max ^
  -drive "file=%QNX_QEMU_DISK%,if=ide,format=raw" ^
  -netdev user,id=net0,hostfwd=tcp::2222-:22,hostfwd=tcp::8000-:8000 ^
  -device virtio-net-pci,netdev=net0 ^
  -nographic ^
  -serial mon:stdio
