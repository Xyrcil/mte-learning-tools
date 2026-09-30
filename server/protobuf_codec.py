"""Schema-driven protobuf encoder for local responses.

The static protocol catalog contains every message field number and nested
type used by the bundled runtime. This encoder supports only those
catalog-described wire forms, so snapshots do not depend on generated C# or
Python classes.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


DEFAULT_PROTOCOL_CATALOG = (
    Path(__file__).resolve().parents[1]
    / "generated"
    / "protocol"
    / "protocol_catalog.json"
)


class ProtocolCodecError(ValueError):
    """Raised when a requested value disagrees with the static schema."""


def encode_varint(value: int) -> bytes:
    value = int(value)
    if value < 0:
        value &= (1 << 64) - 1
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def encode_bytes(field_number: int, value: bytes) -> bytes:
    tag = encode_varint((int(field_number) << 3) | 2)
    return tag + encode_varint(len(value)) + value


def _message_name(type_name: str) -> str:
    return str(type_name).rsplit(".", 1)[-1]


class ProtocolMessageCodec:
    """Encode nested dictionaries using the defined message catalog."""

    def __init__(self, catalog_path: Optional[Path] = None) -> None:
        # The desktop server can use the repository-relative default.  In the
        # embedded Android runtime, however, Python modules are imported from
        # Chaquopy's asset importer while generated data is copied into the
        # app's local files directory.  Keep the no-argument constructor
        # portable by allowing the bootstrap layer to provide that directory.
        configured_path = os.environ.get("MTE_PROTOCOL_CATALOG")
        path = Path(catalog_path or configured_path or DEFAULT_PROTOCOL_CATALOG).resolve()
        document = json.loads(path.read_text(encoding="utf-8"))
        self.messages: Dict[str, Mapping[str, Any]] = {
            str(row["name"]): row for row in document.get("message_types", [])
        }

    def schema(self, message_name: str) -> Mapping[str, Any]:
        name = _message_name(message_name)
        try:
            return self.messages[name]
        except KeyError:
            raise ProtocolCodecError(f"unknown protocol message {name!r}") from None

    def encode(self, message_name: str, values: Mapping[str, Any]) -> bytes:
        if not isinstance(values, Mapping):
            raise ProtocolCodecError(f"{message_name} values must be a mapping")
        schema = self.schema(message_name)
        known_fields = {str(field["name"]): field for field in schema.get("fields", [])}
        unknown = set(values) - set(known_fields)
        if unknown:
            raise ProtocolCodecError(
                f"{message_name} has unknown fields: {sorted(unknown)}"
            )
        body = bytearray()
        for field in schema.get("fields", []):
            field_name = str(field["name"])
            if field_name not in values or values[field_name] is None:
                continue
            body.extend(
                self._encode_field(
                    int(field["number"]),
                    field.get("type", {}),
                    values[field_name],
                    f"{message_name}.{field_name}",
                )
            )
        return bytes(body)

    def _encode_field(
        self,
        field_number: int,
        type_info: Mapping[str, Any],
        value: Any,
        context: str,
    ) -> bytes:
        kind = type_info.get("kind")
        if kind == "repeated":
            if not isinstance(value, (list, tuple)):
                raise ProtocolCodecError(f"{context} must be a sequence")
            arguments = type_info.get("arguments") or []
            if len(arguments) != 1:
                raise ProtocolCodecError(f"{context} has no element schema")
            return b"".join(
                self._encode_field(field_number, arguments[0], item, context)
                for item in value
            )
        if kind == "map":
            if not isinstance(value, Mapping):
                raise ProtocolCodecError(f"{context} must be a mapping")
            arguments = type_info.get("arguments") or []
            if len(arguments) != 2:
                raise ProtocolCodecError(f"{context} has no key/value schema")
            entries = []
            for key in sorted(value, key=lambda item: str(item)):
                entry = self._encode_field(1, arguments[0], key, context)
                entry += self._encode_field(2, arguments[1], value[key], context)
                entries.append(encode_bytes(field_number, entry))
            return b"".join(entries)
        if kind == "message":
            if isinstance(value, Mapping):
                nested = self.encode(str(type_info.get("type") or ""), value)
            elif isinstance(value, (bytes, bytearray, memoryview)):
                nested = bytes(value)
            else:
                raise ProtocolCodecError(f"{context} must be a mapping or bytes")
            return encode_bytes(field_number, nested)
        if kind not in {"scalar", "enum"}:
            raise ProtocolCodecError(f"{context} has unsupported kind {kind!r}")

        scalar = str(type_info.get("scalar") or "")
        if scalar == "string":
            return encode_bytes(field_number, str(value).encode("utf-8"))
        if scalar == "bytes":
            raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
            return encode_bytes(field_number, raw)
        if scalar == "float":
            return encode_varint((field_number << 3) | 5) + struct.pack("<f", float(value))
        if scalar == "double":
            return encode_varint((field_number << 3) | 1) + struct.pack("<d", float(value))
        if scalar == "bool":
            value = 1 if value else 0
        return encode_varint(field_number << 3) + encode_varint(int(value))


__all__ = [
    "DEFAULT_PROTOCOL_CATALOG",
    "ProtocolCodecError",
    "ProtocolMessageCodec",
    "encode_bytes",
    "encode_varint",
]
