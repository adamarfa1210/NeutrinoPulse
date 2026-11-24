@echo off
REM NeutrinoPulse - QNX 8.0 x86_64 QEMU Launcher (2 Cores + SLIRP Networking)
REM Host SSH Port: 2222 -> Target 22 | Host qconn Port: 8000 -> Target 8000
REM Target -> Host UDP Gateway: 10.0.2.2

"C:\Program Files\qemu\qemu-system-x86_64.exe" ^
  -m 1G ^
  -smp 2 ^
  -cpu max ^
  -drive file=C:\QNX\qemu_target\output\disk-qemu,if=ide,format=raw ^
  -netdev user,id=net0,hostfwd=tcp::2222-:22,hostfwd=tcp::8000-:8000 ^
  -device virtio-net-pci,netdev=net0 ^
  -nographic ^
  -serial mon:stdio
