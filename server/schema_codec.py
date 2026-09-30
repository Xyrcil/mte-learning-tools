"""Small schema-driven protobuf codec for application messages.

The generated protocol catalog is the source of truth for field numbers and
types. Domain layers use this codec so nested responses cannot
silently drift when a message contains maps, repeated messages, or fields
whose legacy names do not match the endpoint name.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple


DEFAULT_PROTOCOL_CATALOG = (
    Path(__file__).resolve().parents[1]
    / "generated"
    / "protocol"
    / "protocol_catalog.json"
)


class SchemaCodecError(ValueError):
    """Raised for invalid wire data or a value incompatible with the schema."""


def _message_name(value: Any) -> str:
    return str(value or "").rsplit(".", 1)[-1]


def _varint(value: int) -> bytes:
    value = int(value)
    if value < 0:
        value &= (1 << 64) - 1
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _read_varint(data: bytes, offset: int) -> Tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        current = data[offset]
        offset += 1
        value |= (current & 0x7F) << shift
        if not current & 0x80:
            return value, offset
        shift += 7
    raise SchemaCodecError("invalid or truncated protobuf varint")


def _tag(number: int, wire_type: int) -> bytes:
    return _varint((int(number) << 3) | int(wire_type))


def _length_value(number: int, value: bytes) -> bytes:
    return _tag(number, 2) + _varint(len(value)) + value


def _wire_fields(data: bytes) -> Dict[int, List[Tuple[int, Any]]]:
    result: Dict[int, List[Tuple[int, Any]]] = {}
    offset = 0
    while offset < len(data):
        raw_tag, offset = _read_varint(data, offset)
        number, wire_type = raw_tag >> 3, raw_tag & 7
        if number <= 0:
            raise SchemaCodecError("protobuf field number must be positive")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise SchemaCodecError("truncated fixed64 field")
            value, offset = data[offset:end], end
        elif wire_type == 2:
            size, offset = _read_varint(data, offset)
            end = offset + size
            if end > len(data):
                raise SchemaCodecError("truncated length-delimited field")
            value, offset = data[offset:end], end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise SchemaCodecError("truncated fixed32 field")
            value, offset = data[offset:end], end
        else:
            raise SchemaCodecError(f"unsupported protobuf wire type {wire_type}")
        result.setdefault(number, []).append((wire_type, value))
    return result


class SchemaCodec:
    """Encode and decode message dictionaries through protocol_catalog.json."""

    def __init__(self, catalog_path: Path | None = None) -> None:
        configured = os.environ.get("MTE_PROTOCOL_CATALOG")
        path = Path(catalog_path or configured or DEFAULT_PROTOCOL_CATALOG).resolve()
        catalog = json.loads(path.read_text(encoding="utf-8"))
        self.messages = {
            str(row["name"]): row for row in catalog.get("message_types", [])
        }
        self.endpoints = {
            int(row["request_id"]): row for row in catalog.get("endpoints", [])
        }

    def endpoint(self, command_id: int) -> Mapping[str, Any]:
        try:
            return self.endpoints[int(command_id)]
        except KeyError:
            raise SchemaCodecError(f"unknown endpoint {command_id}") from None

    def request_name(self, command_id: int) -> str:
        return str(self.endpoint(command_id).get("request_name") or "")

    def response_name(self, command_id: int) -> str:
        return str(self.endpoint(command_id).get("response_name") or "")

    def response_id(self, command_id: int) -> int:
        response_id = self.endpoint(command_id).get("response_id")
        if response_id is None:
            raise SchemaCodecError(f"endpoint {command_id} has no response")
        return int(response_id)

    def _schema(self, name: str) -> Mapping[str, Any]:
        simple = _message_name(name)
        try:
            return self.messages[simple]
        except KeyError:
            raise SchemaCodecError(f"unknown message schema {name}") from None

    @staticmethod
    def _field_value(values: Mapping[str, Any], name: str) -> Any:
        if name in values:
            return values[name]
        normalized = name.lower().replace("_", "")
        for key, value in values.items():
            if str(key).lower().replace("_", "") == normalized:
                return value
        return None

    def encode(self, name: str, values: Mapping[str, Any]) -> bytes:
        body = bytearray()
        for field in self._schema(name).get("fields", []):
            value = self._field_value(values, str(field["name"]))
            if value is None:
                continue
            body.extend(self._encode_field(int(field["number"]), field["type"], value))
        return bytes(body)

    def _encode_field(
        self, number: int, type_info: Mapping[str, Any], value: Any
    ) -> bytes:
        kind = str(type_info.get("kind") or "")
        if kind == "repeated":
            arguments = type_info.get("arguments", [])
            element = arguments[0] if arguments else {}
            if isinstance(value, (str, bytes, bytearray)) or not isinstance(
                value, Iterable
            ):
                raise SchemaCodecError(f"repeated field {number} is not iterable")
            return b"".join(self._encode_field(number, element, item) for item in value)
        if kind == "map":
            arguments = type_info.get("arguments", [])
            if len(arguments) != 2 or not isinstance(value, Mapping):
                raise SchemaCodecError(f"map field {number} has invalid value")
            encoded = bytearray()
            for key in sorted(value, key=lambda item: str(item)):
                entry = self._encode_field(1, arguments[0], key)
                entry += self._encode_field(2, arguments[1], value[key])
                encoded.extend(_length_value(number, entry))
            return bytes(encoded)
        if kind == "message":
            if isinstance(value, (bytes, bytearray)):
                nested = bytes(value)
            elif isinstance(value, Mapping):
                nested = self.encode(_message_name(type_info.get("type")), value)
            else:
                raise SchemaCodecError(f"message field {number} has invalid value")
            return _length_value(number, nested)
        scalar = str(type_info.get("scalar") or "")
        if kind == "enum" or scalar in {
            "bool",
            "int32",
            "int64",
            "uint32",
            "uint64",
            "sint32",
            "sint64",
        }:
            integer = int(bool(value)) if scalar == "bool" else int(value)
            if scalar in {"sint32", "sint64"}:
                integer = (integer << 1) ^ (integer >> (31 if scalar == "sint32" else 63))
            return _tag(number, 0) + _varint(integer)
        if scalar == "string":
            return _length_value(number, str(value).encode("utf-8"))
        if scalar == "bytes":
            return _length_value(number, bytes(value))
        if scalar in {"fixed32", "sfixed32", "float"}:
            packed = (
                struct.pack("<f", float(value))
                if scalar == "float"
                else struct.pack("<I", int(value) & 0xFFFFFFFF)
            )
            return _tag(number, 5) + packed
        if scalar in {"fixed64", "sfixed64", "double"}:
            packed = (
                struct.pack("<d", float(value))
                if scalar == "double"
                else struct.pack("<Q", int(value) & ((1 << 64) - 1))
            )
            return _tag(number, 1) + packed
        raise SchemaCodecError(f"unsupported field kind={kind!r} scalar={scalar!r}")

    def decode(self, name: str, data: bytes) -> Dict[str, Any]:
        wire = _wire_fields(bytes(data))
        result: Dict[str, Any] = {}
        for field in self._schema(name).get("fields", []):
            raw_values = wire.get(int(field["number"]), [])
            if not raw_values:
                continue
            type_info = field["type"]
            kind = str(type_info.get("kind") or "")
            if kind == "repeated":
                arguments = type_info.get("arguments", [])
                element = arguments[0] if arguments else {}
                result[field["name"]] = self._decode_repeated_values(
                    element, raw_values
                )
            elif kind == "map":
                arguments = type_info.get("arguments", [])
                mapping: Dict[Any, Any] = {}
                for wire_type, value in raw_values:
                    if wire_type != 2 or len(arguments) != 2:
                        raise SchemaCodecError(f"invalid map field {field['name']}")
                    entry = _wire_fields(value)
                    key_wire, key = entry.get(1, [(0, 0)])[0]
                    value_wire, item = entry.get(2, [(0, 0)])[0]
                    mapping[self._decode_value(arguments[0], key_wire, key)] = (
                        self._decode_value(arguments[1], value_wire, item)
                    )
                result[field["name"]] = mapping
            else:
                result[field["name"]] = self._decode_value(
                    type_info, raw_values[0][0], raw_values[0][1]
                )
        return result

    def _decode_repeated_values(
        self,
        element: Mapping[str, Any],
        raw_values: Iterable[Tuple[int, Any]],
    ) -> List[Any]:
        """Decode both legal protobuf encodings for repeated primitives.

        Generated C# protobuf runtimes use packed (wire type 2) encoding for
        repeated numeric fields by default.  Earlier offline handlers only
        accepted the unpacked representation emitted by this codec's own
        tests, which made real requests such as CS_OpeningChooseHero fail even
        though the defined catalog correctly marked HeroList as repeated.
        """

        kind = str(element.get("kind") or "")
        scalar = str(element.get("scalar") or "")
        varint_scalars = {
            "bool",
            "int32",
            "int64",
            "uint32",
            "uint64",
            "sint32",
            "sint64",
        }
        fixed32_scalars = {"fixed32", "sfixed32", "float"}
        fixed64_scalars = {"fixed64", "sfixed64", "double"}
        packable = kind == "enum" or scalar in (
            varint_scalars | fixed32_scalars | fixed64_scalars
        )

        decoded: List[Any] = []
        for wire_type, value in raw_values:
            if wire_type != 2 or not packable:
                decoded.append(self._decode_value(element, wire_type, value))
                continue
            packed = bytes(value)
            if kind == "enum" or scalar in varint_scalars:
                offset = 0
                while offset < len(packed):
                    integer, offset = _read_varint(packed, offset)
                    decoded.append(self._decode_value(element, 0, integer))
                continue
            width = 4 if scalar in fixed32_scalars else 8
            packed_wire = 5 if width == 4 else 1
            if len(packed) % width:
                raise SchemaCodecError(
                    f"packed repeated {scalar} length is not divisible by {width}"
                )
            for offset in range(0, len(packed), width):
                decoded.append(
                    self._decode_value(
                        element, packed_wire, packed[offset : offset + width]
                    )
                )
        return decoded

    def _decode_value(
        self, type_info: Mapping[str, Any], wire_type: int, value: Any
    ) -> Any:
        kind = str(type_info.get("kind") or "")
        if kind == "message":
            if wire_type != 2:
                raise SchemaCodecError("message value is not length-delimited")
            return self.decode(_message_name(type_info.get("type")), bytes(value))
        scalar = str(type_info.get("scalar") or "")
        if kind == "enum" or wire_type == 0:
            integer = int(value)
            if scalar in {"sint32", "sint64"}:
                integer = (integer >> 1) ^ -(integer & 1)
            elif scalar == "bool":
                return bool(integer)
            return integer
        if scalar == "string" and wire_type == 2:
            return bytes(value).decode("utf-8", errors="replace")
        if scalar == "bytes" and wire_type == 2:
            return bytes(value)
        if scalar == "float" and wire_type == 5:
            return struct.unpack("<f", bytes(value))[0]
        if scalar == "double" and wire_type == 1:
            return struct.unpack("<d", bytes(value))[0]
        if wire_type in {1, 5}:
            return int.from_bytes(bytes(value), "little")
        raise SchemaCodecError(f"wire type {wire_type} does not match {type_info}")

    def decode_request(self, command_id: int, data: bytes) -> Dict[str, Any]:
        return self.decode(self.request_name(command_id), data)

    def encode_response(self, command_id: int, values: Mapping[str, Any]) -> bytes:
        return self.encode(self.response_name(command_id), values)
