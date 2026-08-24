"""Read-only discovery of the WeChat 4.x V2 image key on Windows."""

from __future__ import annotations

import ctypes
import re
import struct
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Iterable, Optional

from .media import IMAGE_SIGNATURES, V2_MAGIC, _aes_ecb

_RUNTIME_KEY: Optional[str] = None
_SCAN_THREAD: Optional[threading.Thread] = None
_CANDIDATE = re.compile(rb"(?<![A-Za-z0-9])(?:[A-Za-z0-9]{16}|[A-Za-z0-9]{32})(?![A-Za-z0-9])")


def runtime_image_key() -> str:
    return _RUNTIME_KEY or ""


def _valid_candidate(candidate: bytes, encrypted_block: bytes) -> Optional[str]:
    for key in (candidate[:16], candidate[-16:]):
        if len(key) != 16:
            continue
        try:
            try:
                from Crypto.Cipher import AES
                plain = AES.new(key, AES.MODE_ECB).decrypt(encrypted_block)
            except ImportError:
                plain = _aes_ecb(encrypted_block, key)
        except Exception:
            continue
        if any(plain.startswith(signature) for signature, _ext, _mime in IMAGE_SIGNATURES):
            return key.decode("ascii")
    return None


def find_key_in_chunks(chunks: Iterable[bytes], encrypted_block: bytes) -> str:
    """Pure validation core, separated for safe tests without a process."""

    seen = set()
    for chunk in chunks:
        for match in _CANDIDATE.finditer(chunk):
            candidate = match.group(0)
            if candidate in seen:
                continue
            seen.add(candidate)
            value = _valid_candidate(candidate, encrypted_block)
            if value:
                return value
    return ""


def _v2_test_block(path: Path) -> bytes:
    raw = path.read_bytes()[:64]
    if not raw.startswith(V2_MAGIC) or len(raw) < 31:
        raise ValueError("需要一张可读的微信 V2 .dat 图片作为校验样本")
    if struct.unpack_from("<I", raw, 6)[0] <= 0:
        raise ValueError("V2 图片 AES 分段无效")
    return raw[15:31]


def _process_ids(names=("Weixin.exe",)) -> list[int]:
    import psutil

    wanted = {name.lower() for name in names}
    values = []
    for proc in psutil.process_iter(("pid", "name", "memory_info")):
        try:
            if str(proc.info.get("name") or "").lower() in wanted:
                rss = int((proc.info.get("memory_info") or [0])[0])
                values.append((rss, int(proc.info["pid"])))
        except (psutil.Error, OSError, TypeError):
            continue
    return [pid for _rss, pid in sorted(values, reverse=True)]


def _memory_chunks(pid: int, chunk_size: int = 1024 * 1024, deadline: Optional[float] = None):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not process:
        return

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
            ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD),
        ]

    kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    address, maximum = 0, (1 << 47) - 1
    readable = {0x02, 0x04, 0x20, 0x40}
    try:
        while address < maximum:
            if deadline is not None and time.monotonic() >= deadline:
                return
            mbi = MEMORY_BASIC_INFORMATION()
            if not kernel32.VirtualQueryEx(process, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            base, size = int(mbi.BaseAddress or address), int(mbi.RegionSize or 0)
            if size <= 0:
                break
            protect = int(mbi.Protect) & 0xFF
            # Session keys live on writable heap pages.  Avoid scanning mapped
            # executables and large read-only assets from the 1GB+ UI process.
            if int(mbi.State) == 0x1000 and int(mbi.Type) == 0x20000 and protect in {0x04, 0x40} and not (int(mbi.Protect) & 0x100):
                offset, carry = 0, b""
                while offset < size:
                    if deadline is not None and time.monotonic() >= deadline:
                        return
                    requested = min(chunk_size, size - offset)
                    buffer, read = ctypes.create_string_buffer(requested), ctypes.c_size_t()
                    ok = kernel32.ReadProcessMemory(process, ctypes.c_void_p(base + offset), buffer, requested, ctypes.byref(read))
                    if ok and read.value:
                        data = carry + buffer.raw[: read.value]
                        yield data
                        carry = data[-40:]
                    offset += requested
            address = base + size
    finally:
        kernel32.CloseHandle(process)


def discover_image_key(sample_path: str, max_seconds: float = 8.0) -> str:
    """Find and retain a validated key in memory; never log or persist it."""

    global _RUNTIME_KEY
    if _RUNTIME_KEY:
        return _RUNTIME_KEY
    block = _v2_test_block(Path(sample_path))
    deadline = time.monotonic() + max(1.0, float(max_seconds))
    for pid in _process_ids()[:2]:
        value = find_key_in_chunks(_memory_chunks(pid, deadline=deadline), block)
        if value:
            _RUNTIME_KEY = value
            return value
        if time.monotonic() >= deadline:
            break
    return ""


def request_image_key_discovery(sample_path: str) -> bool:
    """Start one bounded background scan and return whether a key exists now."""

    global _SCAN_THREAD
    if _RUNTIME_KEY:
        return True
    if _SCAN_THREAD is not None and _SCAN_THREAD.is_alive():
        return False
    _SCAN_THREAD = threading.Thread(
        target=discover_image_key,
        args=(sample_path,),
        name="wechat-image-key-scan",
        daemon=True,
    )
    _SCAN_THREAD.start()
    return False
