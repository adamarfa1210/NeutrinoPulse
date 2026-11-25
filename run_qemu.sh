#!/usr/bin/env sh
# NeutrinoPulse - QNX 8.0 x86_64 QEMU Launcher (Linux/WSL, 2 Cores + SLIRP)
# Host SSH Port: 2222 -> Target 22 | Host qconn Port: 8000 -> Target 8000
set -eu

QEMU_EXE="${QEMU_EXE:-qemu-system-x86_64}"
QNX_QEMU_DISK="${QNX_QEMU_DISK:-$HOME/qnx_qemu/disk-qemu}"

if ! command -v "$QEMU_EXE" >/dev/null 2>&1 && [ ! -x "$QEMU_EXE" ]; then
  echo "[ERROR] QEMU binary not found: $QEMU_EXE. Set QEMU_EXE env var." >&2
  exit 1
fi
if [ ! -f "$QNX_QEMU_DISK" ]; then
  echo "[ERROR] QNX disk image not found: $QNX_QEMU_DISK. Set QNX_QEMU_DISK env var." >&2
  exit 1
fi

exec "$QEMU_EXE" \
  -m 1G \
  -smp 2 \
  -cpu max \
  -drive "file=$QNX_QEMU_DISK,if=ide,format=raw" \
  -netdev user,id=net0,hostfwd=tcp::2222-:22,hostfwd=tcp::8000-:8000 \
  -device virtio-net-pci,netdev=net0 \
  -nographic \
  -serial mon:stdio
