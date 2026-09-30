#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import struct
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from typing import Iterable, Sequence, Tuple


MODULE_PATH = Path(__file__).with_name("merge_embedded_apk.py")
SPEC = importlib.util.spec_from_file_location("merge_embedded_apk", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
merge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = merge
SPEC.loader.exec_module(merge)


def _uleb128(value: int) -> bytes:
    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            byte |= 0x80
        encoded.append(byte)
        if not value:
            return bytes(encoded)


def make_dex(descriptors: Sequence[str]) -> bytes:
    """Build the minimal DEX sections needed by the merger's class scanner."""

    count = len(descriptors)
    header_size = 0x70
    string_ids_off = header_size
    type_ids_off = string_ids_off + count * 4
    class_defs_off = type_ids_off + count * 4
    string_data_start = class_defs_off + count * 32
    data = bytearray(string_data_start)
    string_offsets = []

    for descriptor in descriptors:
        encoded = descriptor.encode("utf-8")
        string_offsets.append(len(data))
        data.extend(_uleb128(len(descriptor)))
        data.extend(encoded)
        data.append(0)

    data[0:8] = b"dex\n035\0"
    struct.pack_into("<I", data, 32, len(data))
    struct.pack_into("<I", data, 36, header_size)
    struct.pack_into("<I", data, 40, 0x12345678)
    struct.pack_into("<II", data, 56, count, string_ids_off if count else 0)
    struct.pack_into("<II", data, 64, count, type_ids_off if count else 0)
    struct.pack_into("<II", data, 96, count, class_defs_off if count else 0)

    for index, string_offset in enumerate(string_offsets):
        struct.pack_into("<I", data, string_ids_off + index * 4, string_offset)
        struct.pack_into("<I", data, type_ids_off + index * 4, index)
        struct.pack_into(
            "<8I",
            data,
            class_defs_off + index * 32,
            index,
            1,
            0xFFFFFFFF,
            0,
            0xFFFFFFFF,
            0,
            0,
            0,
        )
    return bytes(data)


def write_apk(path: Path, entries: Iterable[Tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)


class MergeEmbeddedApkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.base = self.root / "base.apk"
        self.helper = self.root / "helper.apk"
        self.output = self.root / "output.apk"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_base(self, extra: Iterable[Tuple[str, bytes]] = ()) -> None:
        write_apk(
            self.base,
            [
                ("AndroidManifest.xml", b"base manifest"),
                ("resources.arsc", b"base resources"),
                ("res/layout/main.xml", b"base layout"),
                ("classes.dex", make_dex(["Lbase/Primary;"])),
                *extra,
            ],
        )

    def write_helper(self, extra: Iterable[Tuple[str, bytes]] = ()) -> None:
        write_apk(
            self.helper,
            [
                ("AndroidManifest.xml", b"helper manifest"),
                ("resources.arsc", b"helper resources"),
                ("res/layout/helper.xml", b"helper layout"),
                ("classes.dex", make_dex(["Lhelper/Primary;"])),
                ("assets/payload.bin", b"payload"),
                *extra,
            ],
        )

    def test_dynamic_dex_mapping_and_base_resources_are_preserved(self) -> None:
        self.write_base(
            [("classes2.dex", make_dex(["Lbase/Secondary;"]))]
        )
        self.write_helper(
            [("classes2.dex", make_dex(["Lhelper/Secondary;"]))]
        )

        plan = merge.merge_apks(
            self.base,
            self.helper,
            self.output,
            required_helper_entries=("assets/payload.bin",),
        )

        self.assertEqual(
            [(item.source_name, item.target_name) for item in plan.dex_mappings],
            [("classes.dex", "classes3.dex"), ("classes2.dex", "classes4.dex")],
        )
        with zipfile.ZipFile(self.output) as archive:
            names = set(archive.namelist())
            self.assertEqual(archive.read("AndroidManifest.xml"), b"base manifest")
            self.assertEqual(archive.read("resources.arsc"), b"base resources")
            self.assertEqual(archive.read("res/layout/main.xml"), b"base layout")
            self.assertNotIn("res/layout/helper.xml", names)
            self.assertEqual(archive.read("assets/payload.bin"), b"payload")
            self.assertTrue(
                {"classes.dex", "classes2.dex", "classes3.dex", "classes4.dex"}
                .issubset(names)
            )

        merge.verify_merged_apk(
            self.base,
            self.helper,
            self.output,
            required_helper_entries=("assets/payload.bin",),
        )

    def test_duplicate_class_fails_before_existing_output_is_replaced(self) -> None:
        self.write_base()
        write_apk(
            self.helper,
            [
                ("AndroidManifest.xml", b"helper"),
                ("resources.arsc", b"helper"),
                ("classes.dex", make_dex(["Lbase/Primary;"])),
            ],
        )
        self.output.write_bytes(b"existing artifact")

        with self.assertRaisesRegex(merge.MergeError, "duplicate class"):
            merge.merge_apks(self.base, self.helper, self.output)
        self.assertEqual(self.output.read_bytes(), b"existing artifact")

    def test_non_resource_entry_collision_is_rejected(self) -> None:
        self.write_base([("assets/shared.bin", b"base")])
        self.write_helper([("assets/shared.bin", b"helper")])

        with self.assertRaisesRegex(merge.MergeError, "entry conflicts"):
            merge.build_merge_plan(self.base, self.helper, self.output)
        self.assertFalse(self.output.exists())

    def test_duplicate_zip_entry_is_rejected(self) -> None:
        self.write_base()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            write_apk(
                self.helper,
                [
                    ("AndroidManifest.xml", b"helper"),
                    ("resources.arsc", b"helper"),
                    ("classes.dex", make_dex(["Lhelper/Primary;"])),
                    ("assets/duplicate.bin", b"one"),
                    ("assets/duplicate.bin", b"two"),
                ],
            )

        with self.assertRaisesRegex(merge.MergeError, "duplicate ZIP entries"):
            merge.build_merge_plan(self.base, self.helper, self.output)

    def test_missing_required_helper_payload_is_rejected(self) -> None:
        self.write_base()
        self.write_helper()

        with self.assertRaisesRegex(merge.MergeError, "missing required"):
            merge.build_merge_plan(
                self.base,
                self.helper,
                self.output,
                required_helper_entries=("assets/not-built.bin",),
            )

    def test_verify_detects_changed_base_manifest(self) -> None:
        self.write_base()
        self.write_helper()
        merge.merge_apks(self.base, self.helper, self.output)

        tampered = self.root / "tampered.apk"
        with zipfile.ZipFile(self.output) as source, zipfile.ZipFile(
            tampered, "w"
        ) as destination:
            for info in source.infolist():
                payload = source.read(info.filename)
                if info.filename == "AndroidManifest.xml":
                    payload = b"tampered"
                destination.writestr(info, payload)

        with self.assertRaisesRegex(merge.MergeError, "manifest/resource"):
            merge.verify_merged_apk(self.base, self.helper, tampered)


if __name__ == "__main__":
    unittest.main()
