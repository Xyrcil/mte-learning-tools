#!/usr/bin/env python3
"""Safely merge a helper APK into a rebuilt base APK.

The base APK owns its manifest and compiled resources. Helper DEX files are
renumbered after the highest base DEX index, while non-resource helper payloads
are copied under their original names. All conflicts are detected before an
output file is written.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import os
import re
import struct
import sys
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


DEX_NAME_RE = re.compile(r"^classes(?:([2-9]|[1-9][0-9]+))?\.dex$")
HELPER_SKIP_PREFIXES = ("META-INF/", "kotlin/", "res/")
HELPER_SKIP_NAMES = frozenset(("AndroidManifest.xml", "resources.arsc"))
BASE_REQUIRED_NAMES = frozenset(("AndroidManifest.xml", "resources.arsc"))
DEX_HEADER_SIZE = 0x70
DEX_ENDIAN_CONSTANT = 0x12345678
COPY_BUFFER_SIZE = 1024 * 1024


class MergeError(RuntimeError):
    """Raised when APK inputs cannot be merged without ambiguity or damage."""


@dataclass(frozen=True)
class DexMapping:
    source_name: str
    target_name: str
    class_count: int


@dataclass(frozen=True)
class MergePlan:
    base_entries: Tuple[str, ...]
    helper_entries: Tuple[Tuple[str, str], ...]
    dex_mappings: Tuple[DexMapping, ...]
    skipped_helper_entries: Tuple[str, ...]
    base_class_count: int
    helper_class_count: int

    @property
    def expected_entries(self) -> frozenset:
        return frozenset(self.base_entries).union(
            target_name for _, target_name in self.helper_entries
        )


def _dex_index(name: str) -> Optional[int]:
    match = DEX_NAME_RE.fullmatch(name)
    if match is None:
        return None
    return 1 if match.group(1) is None else int(match.group(1))


def _dex_name(index: int) -> str:
    if index < 1:
        raise ValueError("DEX indexes start at one")
    return "classes.dex" if index == 1 else f"classes{index}.dex"


def _is_helper_excluded(name: str) -> bool:
    return name in HELPER_SKIP_NAMES or name.startswith(HELPER_SKIP_PREFIXES)


def _is_base_resource(name: str) -> bool:
    return name in BASE_REQUIRED_NAMES or name.startswith("res/")


def _is_signature_metadata(name: str) -> bool:
    if not name.startswith("META-INF/"):
        return False
    basename = name.rsplit("/", 1)[-1].upper()
    return basename == "MANIFEST.MF" or basename.endswith(
        (".SF", ".RSA", ".DSA", ".EC")
    )


def _validate_entry_names(
    archive: zipfile.ZipFile, label: str
) -> Tuple[str, ...]:
    names = tuple(info.filename for info in archive.infolist())
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicates:
        preview = ", ".join(repr(name) for name in duplicates[:8])
        suffix = " ..." if len(duplicates) > 8 else ""
        raise MergeError(f"{label} APK has duplicate ZIP entries: {preview}{suffix}")

    invalid = sorted(
        name
        for name in names
        if not name
        or name.startswith("/")
        or "\\" in name
        or any(part == ".." for part in name.split("/"))
    )
    if invalid:
        preview = ", ".join(repr(name) for name in invalid[:8])
        suffix = " ..." if len(invalid) > 8 else ""
        raise MergeError(f"{label} APK has unsafe ZIP entry names: {preview}{suffix}")
    return names


def _dex_entries(names: Iterable[str], label: str) -> List[Tuple[int, str]]:
    entries: List[Tuple[int, str]] = []
    for name in names:
        index = _dex_index(name)
        if index is not None:
            entries.append((index, name))
        elif name.startswith("classes") and name.endswith(".dex") and "/" not in name:
            raise MergeError(f"{label} APK has a non-canonical DEX name: {name!r}")

    entries.sort()
    if entries and entries[0][0] != 1:
        raise MergeError(f"{label} APK has secondary DEX files but no classes.dex")
    return entries


def _read_u32(data: bytes, offset: int, label: str) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise MergeError(f"{label}: 32-bit value at offset {offset} is out of bounds")
    return struct.unpack_from("<I", data, offset)[0]


def _read_uleb128(data: bytes, offset: int, label: str) -> Tuple[int, int]:
    value = 0
    for byte_index in range(5):
        if offset >= len(data):
            raise MergeError(f"{label}: truncated ULEB128 value")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << (byte_index * 7)
        if not byte & 0x80:
            return value, offset
    raise MergeError(f"{label}: invalid ULEB128 value")


def _validate_table(
    data: bytes, size: int, offset: int, item_size: int, section: str, label: str
) -> None:
    if size == 0:
        return
    if offset < DEX_HEADER_SIZE or offset > len(data):
        raise MergeError(f"{label}: invalid {section} offset {offset}")
    end = offset + size * item_size
    if end > len(data):
        raise MergeError(f"{label}: {section} extends past the end of the DEX")


def dex_class_descriptors(data: bytes, label: str = "DEX") -> Tuple[bytes, ...]:
    """Return raw class descriptors from a standard little-endian DEX file."""

    if len(data) < DEX_HEADER_SIZE:
        raise MergeError(f"{label}: file is smaller than a DEX header")
    if data[:4] != b"dex\n" or data[7] != 0 or not data[4:7].isdigit():
        raise MergeError(f"{label}: invalid DEX magic")

    file_size = _read_u32(data, 32, label)
    header_size = _read_u32(data, 36, label)
    endian_tag = _read_u32(data, 40, label)
    if file_size != len(data):
        raise MergeError(
            f"{label}: header file size {file_size} does not match {len(data)} bytes"
        )
    if header_size != DEX_HEADER_SIZE:
        raise MergeError(f"{label}: unsupported DEX header size {header_size}")
    if endian_tag != DEX_ENDIAN_CONSTANT:
        raise MergeError(f"{label}: unsupported DEX endianness 0x{endian_tag:08x}")

    string_ids_size = _read_u32(data, 56, label)
    string_ids_off = _read_u32(data, 60, label)
    type_ids_size = _read_u32(data, 64, label)
    type_ids_off = _read_u32(data, 68, label)
    class_defs_size = _read_u32(data, 96, label)
    class_defs_off = _read_u32(data, 100, label)

    _validate_table(data, string_ids_size, string_ids_off, 4, "string_ids", label)
    _validate_table(data, type_ids_size, type_ids_off, 4, "type_ids", label)
    _validate_table(data, class_defs_size, class_defs_off, 32, "class_defs", label)

    descriptors: List[bytes] = []
    for class_number in range(class_defs_size):
        class_idx = _read_u32(data, class_defs_off + class_number * 32, label)
        if class_idx >= type_ids_size:
            raise MergeError(
                f"{label}: class_defs[{class_number}] has invalid type index {class_idx}"
            )
        descriptor_idx = _read_u32(data, type_ids_off + class_idx * 4, label)
        if descriptor_idx >= string_ids_size:
            raise MergeError(
                f"{label}: type_ids[{class_idx}] has invalid string index "
                f"{descriptor_idx}"
            )
        string_data_off = _read_u32(
            data, string_ids_off + descriptor_idx * 4, label
        )
        _, descriptor_off = _read_uleb128(data, string_data_off, label)
        try:
            descriptor_end = data.index(0, descriptor_off)
        except ValueError as exc:
            raise MergeError(f"{label}: unterminated class descriptor") from exc
        descriptor = data[descriptor_off:descriptor_end]
        if not descriptor.startswith(b"L") or not descriptor.endswith(b";"):
            rendered = descriptor.decode("utf-8", "backslashreplace")
            raise MergeError(f"{label}: invalid class descriptor {rendered!r}")
        descriptors.append(descriptor)
    return tuple(descriptors)


def _render_descriptor(descriptor: bytes) -> str:
    return descriptor.decode("utf-8", "backslashreplace")


def _scan_classes(
    archive: zipfile.ZipFile,
    dex_entries: Iterable[Tuple[int, str]],
    label: str,
    origins: Dict[bytes, str],
) -> Tuple[Mapping[str, int], int]:
    counts: Dict[str, int] = {}
    total = 0
    for _, dex_name in dex_entries:
        origin = f"{label}:{dex_name}"
        descriptors = dex_class_descriptors(archive.read(dex_name), origin)
        counts[dex_name] = len(descriptors)
        total += len(descriptors)
        for descriptor in descriptors:
            previous = origins.get(descriptor)
            if previous is not None:
                rendered = _render_descriptor(descriptor)
                raise MergeError(
                    f"duplicate class {rendered} found in {previous} and {origin}"
                )
            origins[descriptor] = origin
    return counts, total


def _validate_paths(base_apk: Path, helper_apk: Path, output_apk: Path) -> None:
    for path, label in ((base_apk, "base"), (helper_apk, "helper")):
        if not path.is_file():
            raise MergeError(f"{label} APK does not exist or is not a file: {path}")

    base_resolved = base_apk.resolve()
    helper_resolved = helper_apk.resolve()
    output_resolved = output_apk.resolve()
    if base_resolved == helper_resolved:
        raise MergeError("base and helper APK paths must be different")
    if output_resolved in (base_resolved, helper_resolved):
        raise MergeError("output APK must not overwrite either input APK")


def build_merge_plan(
    base_apk: Path,
    helper_apk: Path,
    output_apk: Path,
    required_helper_entries: Sequence[str] = (),
) -> MergePlan:
    """Validate both inputs and return a conflict-free merge plan."""

    base_apk = Path(base_apk)
    helper_apk = Path(helper_apk)
    output_apk = Path(output_apk)
    _validate_paths(base_apk, helper_apk, output_apk)

    try:
        with zipfile.ZipFile(base_apk, "r") as base, zipfile.ZipFile(
            helper_apk, "r"
        ) as helper:
            base_names = _validate_entry_names(base, "base")
            helper_names = _validate_entry_names(helper, "helper")

            missing_base = sorted(BASE_REQUIRED_NAMES.difference(base_names))
            if missing_base:
                raise MergeError(
                    f"base APK is missing authoritative entries: {missing_base}"
                )

            base_dex = _dex_entries(base_names, "base")
            helper_dex = _dex_entries(helper_names, "helper")
            if not helper_dex:
                raise MergeError("helper APK has no classes.dex")

            class_origins: Dict[bytes, str] = {}
            _, base_class_count = _scan_classes(
                base, base_dex, "base", class_origins
            )
            helper_class_counts, helper_class_count = _scan_classes(
                helper, helper_dex, "helper", class_origins
            )

            next_dex_index = max((index for index, _ in base_dex), default=0) + 1
            dex_targets: Dict[str, str] = {}
            dex_mappings: List[DexMapping] = []
            for offset, (_, source_name) in enumerate(helper_dex):
                target_name = _dex_name(next_dex_index + offset)
                dex_targets[source_name] = target_name
                dex_mappings.append(
                    DexMapping(
                        source_name,
                        target_name,
                        helper_class_counts[source_name],
                    )
                )

            existing = set(base_names)
            included: List[Tuple[str, str]] = []
            skipped: List[str] = []
            for source_name in helper_names:
                target_name = dex_targets.get(source_name)
                if target_name is None:
                    if _is_helper_excluded(source_name):
                        skipped.append(source_name)
                        continue
                    target_name = source_name

                if target_name in existing:
                    raise MergeError(
                        "helper entry conflicts with base or another helper entry: "
                        f"{source_name!r} -> {target_name!r}"
                    )
                existing.add(target_name)
                included.append((source_name, target_name))

            included_sources = {source for source, _ in included}
            missing_required = sorted(
                entry
                for entry in required_helper_entries
                if entry not in included_sources
            )
            if missing_required:
                raise MergeError(
                    "helper APK is missing required mergeable entries: "
                    f"{missing_required}"
                )

            return MergePlan(
                base_entries=base_names,
                helper_entries=tuple(included),
                dex_mappings=tuple(dex_mappings),
                skipped_helper_entries=tuple(skipped),
                base_class_count=base_class_count,
                helper_class_count=helper_class_count,
            )
    except zipfile.BadZipFile as exc:
        raise MergeError(f"invalid APK ZIP: {exc}") from exc


def _copy_entry(
    destination: zipfile.ZipFile,
    source: zipfile.ZipFile,
    source_name: str,
    target_name: Optional[str] = None,
) -> None:
    info = copy.copy(source.getinfo(source_name))
    if target_name is not None:
        info.filename = target_name
        info.orig_filename = target_name
    destination.writestr(info, source.read(source_name))


def _entry_digest(archive: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with archive.open(name, "r") as entry:
        while True:
            chunk = entry.read(COPY_BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _same_entry(
    left: zipfile.ZipFile,
    left_name: str,
    right: zipfile.ZipFile,
    right_name: str,
    cryptographic: bool = False,
) -> bool:
    left_info = left.getinfo(left_name)
    right_info = right.getinfo(right_name)
    if (
        left_info.file_size != right_info.file_size
        or left_info.CRC != right_info.CRC
    ):
        return False
    if cryptographic:
        return _entry_digest(left, left_name) == _entry_digest(right, right_name)
    return True


def verify_merged_apk(
    base_apk: Path,
    helper_apk: Path,
    output_apk: Path,
    plan: Optional[MergePlan] = None,
    required_helper_entries: Sequence[str] = (),
    allow_signing_metadata_changes: bool = True,
) -> MergePlan:
    """Verify payload, DEX classes, and base-owned resources in an output APK."""

    base_apk = Path(base_apk)
    helper_apk = Path(helper_apk)
    output_apk = Path(output_apk)
    if plan is None:
        plan = build_merge_plan(
            base_apk, helper_apk, output_apk, required_helper_entries
        )
    if not output_apk.is_file():
        raise MergeError(f"output APK does not exist or is not a file: {output_apk}")

    try:
        with zipfile.ZipFile(base_apk, "r") as base, zipfile.ZipFile(
            helper_apk, "r"
        ) as helper, zipfile.ZipFile(output_apk, "r") as output:
            output_names_tuple = _validate_entry_names(output, "output")
            output_names = set(output_names_tuple)
            expected_names = set(plan.expected_entries)

            missing = sorted(
                name
                for name in expected_names.difference(output_names)
                if not (
                    allow_signing_metadata_changes and _is_signature_metadata(name)
                )
            )
            extras = sorted(
                name
                for name in output_names.difference(expected_names)
                if not (
                    allow_signing_metadata_changes and _is_signature_metadata(name)
                )
            )
            if missing:
                raise MergeError(f"output APK is missing expected entries: {missing[:8]}")
            if extras:
                raise MergeError(f"output APK has unexpected entries: {extras[:8]}")

            for name in plan.base_entries:
                if _is_signature_metadata(name) and allow_signing_metadata_changes:
                    continue
                if name not in output_names:
                    raise MergeError(f"output APK lost base entry: {name!r}")
                if not _same_entry(
                    base, name, output, name, cryptographic=_is_base_resource(name)
                ):
                    ownership = "manifest/resource" if _is_base_resource(name) else "entry"
                    raise MergeError(f"output APK changed base {ownership}: {name!r}")

            required_set = set(required_helper_entries)
            for source_name, target_name in plan.helper_entries:
                if target_name not in output_names:
                    raise MergeError(
                        f"output APK lost helper entry: {source_name!r} -> "
                        f"{target_name!r}"
                    )
                cryptographic = (
                    source_name in required_set or _dex_index(source_name) is not None
                )
                if not _same_entry(
                    helper,
                    source_name,
                    output,
                    target_name,
                    cryptographic=cryptographic,
                ):
                    raise MergeError(
                        f"output APK changed helper entry: {source_name!r} -> "
                        f"{target_name!r}"
                    )

            output_dex = _dex_entries(output_names_tuple, "output")
            _scan_classes(output, output_dex, "output", {})
            return plan
    except zipfile.BadZipFile as exc:
        raise MergeError(f"invalid APK ZIP: {exc}") from exc


def merge_apks(
    base_apk: Path,
    helper_apk: Path,
    output_apk: Path,
    required_helper_entries: Sequence[str] = (),
) -> MergePlan:
    """Merge into an atomic output after a complete conflict preflight."""

    base_apk = Path(base_apk)
    helper_apk = Path(helper_apk)
    output_apk = Path(output_apk)
    plan = build_merge_plan(
        base_apk, helper_apk, output_apk, required_helper_entries
    )

    output_apk.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_apk.name}.", suffix=".tmp", dir=str(output_apk.parent)
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with zipfile.ZipFile(base_apk, "r") as base, zipfile.ZipFile(
            helper_apk, "r"
        ) as helper, zipfile.ZipFile(
            temporary_path, "w", allowZip64=True
        ) as output:
            output.comment = base.comment
            for source_name in plan.base_entries:
                _copy_entry(output, base, source_name)
            for source_name, target_name in plan.helper_entries:
                _copy_entry(output, helper, source_name, target_name)

        verify_merged_apk(
            base_apk,
            helper_apk,
            temporary_path,
            plan=plan,
            required_helper_entries=required_helper_entries,
            allow_signing_metadata_changes=False,
        )
        os.replace(str(temporary_path), str(output_apk))
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return plan


def merge_fixed(base_apk: Path, helper_apk: Path, output_apk: Path) -> None:
    """Backward-compatible entry point retained for existing callers."""

    merge_apks(base_apk, helper_apk, output_apk)


def _print_plan(plan: MergePlan) -> None:
    for mapping in plan.dex_mappings:
        print(
            f"DEX {mapping.source_name} -> {mapping.target_name} "
            f"({mapping.class_count} classes)"
        )
    print(
        f"validated {plan.base_class_count} base classes, "
        f"{plan.helper_class_count} helper classes, "
        f"{len(plan.helper_entries)} helper entries"
    )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Merge helper DEX/native/assets into a base APK while preserving "
            "the base manifest and resources."
        )
    )
    parser.add_argument("base_apk", type=Path)
    parser.add_argument("helper_apk", type=Path)
    parser.add_argument("output_apk", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="validate inputs and print the DEX mapping without writing output",
    )
    mode.add_argument(
        "--verify-only",
        action="store_true",
        help="verify an existing merged/signed output without modifying it",
    )
    parser.add_argument(
        "--require-helper-entry",
        action="append",
        default=[],
        metavar="ZIP_PATH",
        help="require a helper payload entry (repeatable)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _argument_parser()
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            plan = build_merge_plan(
                args.base_apk,
                args.helper_apk,
                args.output_apk,
                args.require_helper_entry,
            )
            _print_plan(plan)
            print("dry-run OK; no output written")
        elif args.verify_only:
            plan = verify_merged_apk(
                args.base_apk,
                args.helper_apk,
                args.output_apk,
                required_helper_entries=args.require_helper_entry,
            )
            _print_plan(plan)
            print(f"verified {args.output_apk}")
        else:
            plan = merge_apks(
                args.base_apk,
                args.helper_apk,
                args.output_apk,
                args.require_helper_entry,
            )
            _print_plan(plan)
            print(f"wrote {args.output_apk}")
    except (MergeError, OSError, zipfile.LargeZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
