"""Safe local media resolution for the workbench.

The resolver never accepts an arbitrary browser path.  It only decodes a
message's already indexed media path and returns bytes to the loopback server.
Legacy and V1 WeChat images can be decoded locally; V2 images need the key
which WeChat keeps in its live process and therefore return an explicit,
actionable unavailable state when no key has been configured.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import struct
from collections import Counter
from pathlib import Path
from typing import Optional, Tuple


V1_MAGIC = b"\x07\x08V1\x08\x07"
V2_MAGIC = b"\x07\x08V2\x08\x07"
IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"GIF8", "gif", "image/gif"),
    (b"RIFF", "webp", "image/webp"),
    (b"BM", "bmp", "image/bmp"),
)


class MediaUnavailable(RuntimeError):
    """The indexed media exists but cannot currently be rendered."""


def _format_for(data: bytes) -> Optional[Tuple[str, str]]:
    for signature, extension, content_type in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return extension, content_type
    if data[:4] in {b"II*\x00", b"MM\x00*"}:
        return "tif", "image/tiff"
    return None


def _xor(data: bytes, key: int) -> bytes:
    return bytes(value ^ key for value in data)


def _old_xor_key(data: bytes) -> Optional[int]:
    known = (
        b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF", b"BM",
    )
    for signature in known:
        if len(data) < len(signature):
            continue
        key = data[0] ^ signature[0]
        if all((data[index] ^ key) == byte for index, byte in enumerate(signature)):
            return key
    return None


def _sibling_xor_key(path: Path) -> Optional[int]:
    """Infer the V2 tail XOR key from recent JPEG thumbnails when possible."""

    pairs = Counter()
    try:
        siblings = sorted(
            path.parent.glob("*_t.dat"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )[:64]
    except OSError:
        siblings = []
    for sibling in siblings:
        try:
            with sibling.open("rb") as handle:
                header = handle.read(6)
                handle.seek(-2, os.SEEK_END)
                tail = handle.read(2)
            if header not in {V1_MAGIC, V2_MAGIC} or len(tail) != 2:
                continue
            key_a = tail[0] ^ 0xFF
            key_b = tail[1] ^ 0xD9
            if key_a == key_b:
                pairs[key_a] += 1
        except (OSError, ValueError):
            continue
    return pairs.most_common(1)[0][0] if pairs else None


def _unpad_aes(data: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives import padding

        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(data) + unpadder.finalize()
    except Exception:
        return data.rstrip(b"\x00")


def _aes_ecb(data: bytes, key: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        cipher = Cipher(algorithms.AES(key[:16]), modes.ECB())
        decryptor = cipher.decryptor()
        return decryptor.update(data) + decryptor.finalize()
    except Exception as exc:
        raise MediaUnavailable("本机缺少 AES 解码依赖：%s" % exc) from exc


def _aes_key_bytes(value: str) -> bytes:
    """Accept the two common local key forms: raw 16-byte text or 32-char hex."""

    text = str(value or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{32}", text):
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    return text.encode("ascii", "ignore")


def decode_dat(path: Path, aes_key: str = "", xor_key: Optional[int] = None) -> Tuple[bytes, str, str]:
    raw = path.read_bytes()
    if raw.startswith(V1_MAGIC) or raw.startswith(V2_MAGIC):
        if raw.startswith(V2_MAGIC) and not aes_key:
            raise MediaUnavailable("这是微信 V2 加密图片；需要先取得微信进程中的图片 AES key")
        if len(raw) < 15:
            raise MediaUnavailable("微信图片缓存头不完整")
        aes_size = struct.unpack_from("<I", raw, 6)[0]
        xor_size = struct.unpack_from("<I", raw, 10)[0]
        # WeChat keeps a full PKCS7 block when aes_size is already aligned.
        aligned_aes_size = ((aes_size // 16) + 1) * 16
        start = 15
        end = start + aligned_aes_size
        if end > len(raw) or not aes_size:
            raise MediaUnavailable("微信图片缓存分段长度无效")
        key = b"cfcd208495d565ef" if raw.startswith(V1_MAGIC) else _aes_key_bytes(aes_key)
        if len(key) < 16:
            raise MediaUnavailable("图片 AES key 长度不足 16 字节")
        plain_head = _unpad_aes(_aes_ecb(raw[start:end], key))
        tail = raw[end:]
        if xor_size:
            tail = _xor(tail[-xor_size:], xor_key if xor_key is not None else (_sibling_xor_key(path) or 0x88))
        data = plain_head + tail
        detected = _format_for(data)
        if not detected:
            raise MediaUnavailable("图片已读取但解码后不是可识别的图片格式")
        return data, detected[0], detected[1]

    key = _old_xor_key(raw[:32])
    if key is None:
        raise MediaUnavailable("未识别的微信媒体缓存格式")
    data = _xor(raw, key)
    detected = _format_for(data)
    if not detected:
        raise MediaUnavailable("旧版图片 XOR 解码失败")
    return data, detected[0], detected[1]


def read_media(path_value: str, aes_key: str = "", xor_key: Optional[int] = None) -> Tuple[bytes, str, str]:
    path = Path(str(path_value or "")).expanduser()
    if not path.is_file():
        raise MediaUnavailable("媒体文件不存在，当前仅能回到已记录路径")
    suffix = path.suffix.lower()
    if suffix == ".dat":
        return decode_dat(path, aes_key=aes_key, xor_key=xor_key)
    content_type = mimetypes.guess_type(path.name)[0]
    if not content_type or not content_type.startswith("image/"):
        raise MediaUnavailable("当前媒体不是可直接展示的图片")
    data = path.read_bytes()
    detected = _format_for(data)
    if not detected:
        raise MediaUnavailable("图片文件格式无法识别")
    return data, detected[0], detected[1]


def cache_key(path_value: str, extension: str) -> str:
    try:
        stat = Path(path_value).stat()
        seed = "%s:%s:%s" % (path_value, stat.st_size, stat.st_mtime_ns)
    except OSError:
        seed = path_value
    return hashlib.sha256(seed.encode("utf-8", "ignore")).hexdigest() + "." + extension
