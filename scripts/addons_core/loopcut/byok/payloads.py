"""Small shared binary helpers; vendor adapters only receive the explicitly selected files."""

import base64
import binascii

from .http import ByokError


def data_uri(file) -> str:
    data, mime = file
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def decode_base64(value) -> bytes:
    if not isinstance(value, str):
        raise ByokError("vendor_failed", "Vendor output has no encoded media")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as ex:
        raise ByokError("vendor_failed", "Vendor output has invalid encoded media") from ex
    if not data:
        raise ByokError("vendor_failed", "Vendor output is empty")
    return data


def artifact(name, mime, data, **meta) -> dict:
    if not isinstance(data, bytes) or not data:
        raise ByokError("vendor_failed", "Vendor output is empty")
    return {"name": name, "mime": mime, "data": data, "meta": meta}
