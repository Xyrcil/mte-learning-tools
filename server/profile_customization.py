"""Persistent avatar and avatar-frame state for the offline profile screen.

The source runtime owns the presentation and selection UI, but it expects the
service to provide three authoritative pieces of state:

* every owned ``HeadImage`` returned by command 347;
* the selected image echoed by command 115 and ``SC_SelectRoleRet``;
* one ``GoblinHeadframe`` progression row per owned frame group, plus the
  selected frame ID restored by ``SC_SelectRoleRet``.

The generated endpoint runtime can encode successful scalar responses, but it
cannot mutate or restore any of those values.  This module keeps the feature
storage-agnostic: handlers receive a detached profile and return a pure
top-level patch that the server commits atomically.
"""

from __future__ import annotations

import copy
import json
from collections import defaultdict
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, NamedTuple, Optional, Tuple

try:  # Package imports and direct server execution are both supported.
    from .protobuf_codec import ProtocolMessageCodec
    from .shop_content import (
        DEFAULT_PROTOCOL_CATALOG,
        RESOURCE_HEAD_FRAME,
        RESOURCE_HEAD_IMAGE,
        ShopCatalog,
    )
except ImportError:  # pragma: no cover - direct script compatibility
    from protobuf_codec import ProtocolMessageCodec  # type: ignore
    from shop_content import (  # type: ignore
        DEFAULT_PROTOCOL_CATALOG,
        RESOURCE_HEAD_FRAME,
        RESOURCE_HEAD_IMAGE,
        ShopCatalog,
    )


CS_CHANGE_IMAGE = 115
SC_CHANGE_IMAGE_RET = 116
CS_REQUEST_HEAD_IMAGE_DATAS = 347
SC_HEAD_IMAGE_DATAS_RET = 348

SC_GOBLIN_HEADFRAME_DATAS = 1050
NOTIFY_UPDATE_GOBLIN_HEADFRAME = 1051
CS_USE_GOBLIN_HEADFRAME = 1052
SC_USE_GOBLIN_HEADFRAME_RET = 1053
CS_SET_GOBLIN_HEADFRAME_FAKE_LEVEL = 1054
SC_SET_GOBLIN_HEADFRAME_FAKE_LEVEL_RET = 1055

RESPONSE_IDS = {
    CS_CHANGE_IMAGE: SC_CHANGE_IMAGE_RET,
    CS_REQUEST_HEAD_IMAGE_DATAS: SC_HEAD_IMAGE_DATAS_RET,
    CS_USE_GOBLIN_HEADFRAME: SC_USE_GOBLIN_HEADFRAME_RET,
    CS_SET_GOBLIN_HEADFRAME_FAKE_LEVEL: SC_SET_GOBLIN_HEADFRAME_FAKE_LEVEL_RET,
}

DEFAULT_HEAD_IMAGE_ID = 7_000_001
PROFILE_CUSTOMIZATION_REVISION = 1

ERROR_SUCCESS = 0
ERROR_CLIENT_REQUEST_PARAM = 32_000
ERROR_MESSAGE_DATA_ILLEGAL = 32_769

UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1

EXPECTED_COMMANDS = {
    CS_CHANGE_IMAGE: "CS_ChangeImage",
    SC_CHANGE_IMAGE_RET: "SC_ChangeImageRet",
    CS_REQUEST_HEAD_IMAGE_DATAS: "CS_RequestHeadImageDatas",
    SC_HEAD_IMAGE_DATAS_RET: "SC_HeadImageDatasRet",
    SC_GOBLIN_HEADFRAME_DATAS: "SC_GoblinHeadframeDatas",
    NOTIFY_UPDATE_GOBLIN_HEADFRAME: "Notify_UpdateGoblinHeadframe",
    CS_USE_GOBLIN_HEADFRAME: "CS_UseGoblinHeadframe",
    SC_USE_GOBLIN_HEADFRAME_RET: "SC_UseGoblinHeadframeRet",
    CS_SET_GOBLIN_HEADFRAME_FAKE_LEVEL: "CS_SetGoblinHeadframeFakeLevel",
    SC_SET_GOBLIN_HEADFRAME_FAKE_LEVEL_RET: "SC_SetGoblinHeadframeFakeLevelRet",
}

EXPECTED_PROTOCOL_FIELDS = {
    "CS_ChangeImage": {1: "RoleId", 2: "ImageId"},
    "SC_ChangeImageRet": {1: "Errorcode", 2: "RoleId", 3: "ImageId"},
    "CS_RequestHeadImageDatas": {},
    "SC_HeadImageDatasRet": {1: "HeadImageList"},
    "HeadImage": {1: "HeadImageId", 2: "ExpireTime"},
    "GoblinHeadframe": {
        1: "GroupId",
        2: "Level",
        3: "Exp",
        4: "FakeLevel",
        5: "Id",
    },
    "SC_GoblinHeadframeDatas": {1: "GoblinHeadframeList"},
    "Notify_UpdateGoblinHeadframe": {
        1: "GoblinHeadframeList",
        2: "RoleHeadframe",
    },
    "CS_UseGoblinHeadframe": {1: "Id"},
    "SC_UseGoblinHeadframeRet": {1: "Errorcode", 2: "Id"},
    "CS_SetGoblinHeadframeFakeLevel": {1: "Id", 2: "FakeLevel"},
    "SC_SetGoblinHeadframeFakeLevelRet": {
        1: "Errorcode",
        2: "Id",
        3: "FakeLevel",
    },
}


class ProfileCustomizationConfigError(ValueError):
    """Raised when the pinned static catalog or protocol no longer matches."""


class ProfileCustomizationRequestError(ValueError):
    """Raised internally for malformed protobuf request bodies."""


@dataclass(frozen=True)
class ProfileCustomizationResult:
    """One response plus a shallow-mergeable profile patch."""

    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    reason: str = ""
    notifications: Tuple[Tuple[int, bytes], ...] = ()

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


class InitialSnapshot(NamedTuple):
    command_id: int
    body: bytes

    @property
    def response_id(self) -> int:
        return self.command_id


WireValue = Tuple[int, object]


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
    raise ProfileCustomizationRequestError("invalid or truncated protobuf varint")


def _varint_fields(data: bytes) -> Dict[int, List[int]]:
    fields: DefaultDict[int, List[int]] = defaultdict(list)
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if number <= 0:
            raise ProfileCustomizationRequestError("protobuf field number zero")
        if wire_type != 0:
            raise ProfileCustomizationRequestError(
                f"protobuf field {number} must be a varint"
            )
        value, offset = _read_varint(data, offset)
        fields[number].append(value)
    return dict(fields)


def _single_uint(
    fields: Mapping[int, Sequence[int]],
    number: int,
    *,
    required: bool = True,
    maximum: int = UINT64_MAX,
) -> int:
    values = fields.get(number, ())
    if len(values) > 1:
        raise ProfileCustomizationRequestError(f"protobuf field {number} repeats")
    if not values:
        if required:
            raise ProfileCustomizationRequestError(
                f"protobuf field {number} is required"
            )
        return 0
    value = int(values[0])
    if value < 0 or value > maximum:
        raise ProfileCustomizationRequestError(
            f"protobuf field {number} is outside uint range"
        )
    return value


def _uint(value: Any, default: int = 0, maximum: int = UINT64_MAX) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if 0 <= parsed <= maximum else default


class ProfileCustomizationLayer:
    """Catalog-backed avatar/frame ownership, selection, and persistence."""

    def __init__(
        self,
        catalog: Optional[ShopCatalog] = None,
        *,
        codec: Optional[ProtocolMessageCodec] = None,
        protocol_catalog: Path = DEFAULT_PROTOCOL_CATALOG,
    ) -> None:
        self.catalog = catalog or ShopCatalog()
        self.protocol_catalog = Path(protocol_catalog).expanduser().resolve()
        self.codec = codec or ProtocolMessageCodec(self.protocol_catalog)
        self.avatar_ids = tuple(sorted(int(value) for value in self.catalog.player_avatars))
        self.avatar_id_set = frozenset(self.avatar_ids)
        self.frame_by_id = {
            int(frame_id): dict(row)
            for frame_id, row in self.catalog.head_frames.items()
        }
        groups: DefaultDict[int, List[Dict[str, Any]]] = defaultdict(list)
        for row in self.frame_by_id.values():
            groups[int(row["group"])].append(row)
        self.frame_groups = {
            group_id: tuple(
                sorted(rows, key=lambda row: (int(row["lv"]), int(row["id"])))
            )
            for group_id, rows in sorted(groups.items())
        }
        if DEFAULT_HEAD_IMAGE_ID not in self.avatar_id_set:
            raise ProfileCustomizationConfigError(
                f"default avatar {DEFAULT_HEAD_IMAGE_ID} is absent"
            )
        self._validate_static_catalog()
        self._validate_protocol()

    def _validate_static_catalog(self) -> None:
        # 165 is the defined 1.0 floor.  A compatibility catalog may append
        # verified 2.0 skin-avatar rows; rejecting every count above the floor
        # would make those runtime-table additions unreachable to the profile
        # screen even though their IDs and resources are fully validated.
        if len(self.avatar_ids) < 165:
            raise ProfileCustomizationConfigError(
                f"expected at least 165 player avatars, found {len(self.avatar_ids)}"
            )
        if len(self.frame_by_id) != 32:
            raise ProfileCustomizationConfigError(
                f"expected 32 head frames, found {len(self.frame_by_id)}"
            )
        for frame_id, row in self.frame_by_id.items():
            group_id = _uint(row.get("group"), maximum=UINT32_MAX)
            level = _uint(row.get("lv"), maximum=UINT32_MAX)
            if not group_id or frame_id <= 0:
                raise ProfileCustomizationConfigError(
                    f"invalid a_headframe row {frame_id}"
                )
            duplicates = [
                candidate
                for candidate in self.frame_groups[group_id]
                if int(candidate["lv"]) == level
            ]
            if len(duplicates) != 1:
                raise ProfileCustomizationConfigError(
                    f"a_headframe group {group_id} duplicates level {level}"
                )

    def _validate_protocol(self) -> None:
        try:
            document = json.loads(self.protocol_catalog.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProfileCustomizationConfigError(
                f"cannot load {self.protocol_catalog}"
            ) from exc
        commands = {int(row["id"]): row for row in document.get("commands", [])}
        for command_id, expected_name in EXPECTED_COMMANDS.items():
            if expected_name not in commands.get(command_id, {}).get("names", []):
                raise ProfileCustomizationConfigError(
                    f"protocol command {command_id} is not {expected_name}"
                )
        pairs = document.get("request_to_response", {})
        for request_id, response_id in RESPONSE_IDS.items():
            if int(pairs.get(str(request_id), -1)) != response_id:
                raise ProfileCustomizationConfigError(
                    f"protocol request {request_id} does not map to {response_id}"
                )
        for message_name, expected in EXPECTED_PROTOCOL_FIELDS.items():
            schema = self.codec.schema(message_name)
            actual = {
                int(row["number"]): str(row["name"])
                for row in schema.get("fields", [])
            }
            if actual != expected:
                raise ProfileCustomizationConfigError(
                    f"protocol fields for {message_name} changed: {actual}"
                )

    @staticmethod
    def _resource_owned(profile: Mapping[str, Any], res_type: int, item_id: int) -> bool:
        balances = profile.get("resource_balances")
        if not isinstance(balances, Mapping):
            return False
        return _uint(balances.get(f"{res_type}:{item_id}"), 0) > 0

    def _owned_avatar_ids(self, profile: Mapping[str, Any]) -> List[int]:
        owned = set()
        raw = profile.get("head_image_ids")
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            owned.update(
                value
                for item in raw
                if (value := _uint(item, maximum=UINT32_MAX)) in self.avatar_id_set
            )
        owned.update(
            avatar_id
            for avatar_id in self.avatar_ids
            if self._resource_owned(profile, RESOURCE_HEAD_IMAGE, avatar_id)
        )
        if profile.get("all_content_unlocked") is True:
            owned.update(self.avatar_ids)
        # The base self-portrait is explicitly marked as default-unlocked.
        owned.add(DEFAULT_HEAD_IMAGE_ID)
        return sorted(owned)

    def _source_frame_states(self, profile: Mapping[str, Any]) -> Dict[int, Mapping[str, Any]]:
        raw = profile.get("goblin_headframes")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            return {}
        result: Dict[int, Mapping[str, Any]] = {}
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            group_id = _uint(
                item.get("group_id", item.get("GroupId")),
                maximum=UINT32_MAX,
            )
            if group_id in self.frame_groups and group_id not in result:
                result[group_id] = item
        return result

    def _owned_frame_ids_from_states(
        self, states: Sequence[Mapping[str, Any]]
    ) -> List[int]:
        by_group = {
            _uint(row.get("group_id"), maximum=UINT32_MAX): _uint(
                row.get("level"), maximum=UINT32_MAX
            )
            for row in states
        }
        result = []
        for frame_id, row in self.frame_by_id.items():
            group_id = int(row["group"])
            level = int(row["lv"])
            if group_id in by_group and level <= by_group[group_id]:
                result.append(frame_id)
        return sorted(result)

    def _frame_states(self, profile: Mapping[str, Any]) -> List[Dict[str, int]]:
        existing = self._source_frame_states(profile)
        all_unlocked = profile.get("all_content_unlocked") is True
        result: List[Dict[str, int]] = []
        for group_id, rows in self.frame_groups.items():
            levels = tuple(int(row["lv"]) for row in rows)
            highest_static = levels[-1]
            source = existing.get(group_id)
            purchased_levels = [
                int(row["lv"])
                for row in rows
                if self._resource_owned(profile, RESOURCE_HEAD_FRAME, int(row["id"]))
            ]
            if all_unlocked:
                unlocked_level: Optional[int] = highest_static
            elif source is not None:
                candidate = _uint(
                    source.get("level", source.get("Level")),
                    maximum=UINT32_MAX,
                )
                unlocked_level = candidate if candidate in levels else min(levels)
                if purchased_levels:
                    unlocked_level = max(unlocked_level, max(purchased_levels))
            elif purchased_levels:
                unlocked_level = max(purchased_levels)
            else:
                continue

            available = [row for row in rows if int(row["lv"]) <= unlocked_level]
            if not available:  # Defensive; every admitted level is static.
                continue
            available_by_level = {int(row["lv"]): row for row in available}
            source_fake = (
                _uint(
                    source.get("fake_level", source.get("FakeLevel")),
                    maximum=UINT32_MAX,
                )
                if source is not None
                else unlocked_level
            )
            fake_level = (
                source_fake if source_fake in available_by_level else unlocked_level
            )
            selected_row = available_by_level[fake_level]
            source_id = (
                _uint(source.get("id", source.get("Id")), maximum=UINT32_MAX)
                if source is not None
                else 0
            )
            if source_id in self.frame_by_id:
                source_config = self.frame_by_id[source_id]
                if (
                    int(source_config["group"]) == group_id
                    and int(source_config["lv"]) <= unlocked_level
                ):
                    selected_row = source_config
                    fake_level = int(source_config["lv"])
            result.append(
                {
                    "group_id": group_id,
                    "level": int(unlocked_level),
                    "exp": _uint(
                        source.get("exp", source.get("Exp")),
                        maximum=UINT64_MAX,
                    )
                    if source is not None
                    else 0,
                    "fake_level": fake_level,
                    "id": int(selected_row["id"]),
                }
            )
        return result

    def seed_profile(
        self, profile: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        """Normalize ownership while preserving every valid user selection."""

        if not isinstance(profile, MutableMapping):
            raise TypeError("profile must be a mutable mapping")
        avatar_ids = self._owned_avatar_ids(profile)
        selected_avatar = _uint(profile.get("head_image_id"), maximum=UINT32_MAX)
        if selected_avatar not in avatar_ids:
            selected_avatar = DEFAULT_HEAD_IMAGE_ID

        frame_states = self._frame_states(profile)
        frame_ids = self._owned_frame_ids_from_states(frame_states)
        selected_frame = _uint(profile.get("head_frame_id"), maximum=UINT32_MAX)
        if selected_frame not in frame_ids:
            selected_frame = 0

        profile["head_image_ids"] = avatar_ids
        profile["head_image_id"] = selected_avatar
        profile["goblin_headframes"] = frame_states
        profile["head_frame_ids"] = frame_ids
        profile["head_frame_id"] = selected_frame
        profile["profile_customization_revision"] = PROFILE_CUSTOMIZATION_REVISION
        return profile

    @staticmethod
    def _protocol_frame(row: Mapping[str, Any]) -> Dict[str, int]:
        return {
            "GroupId": int(row["group_id"]),
            "Level": int(row["level"]),
            "Exp": int(row["exp"]),
            "FakeLevel": int(row["fake_level"]),
            "Id": int(row["id"]),
        }

    def make_head_image_datas_ret(self, profile: Mapping[str, Any]) -> bytes:
        return self.codec.encode(
            "SC_HeadImageDatasRet",
            {
                "HeadImageList": [
                    {"HeadImageId": avatar_id, "ExpireTime": 0}
                    for avatar_id in self._owned_avatar_ids(profile)
                ]
            },
        )

    def make_goblin_headframe_datas(self, profile: Mapping[str, Any]) -> bytes:
        return self.codec.encode(
            "SC_GoblinHeadframeDatas",
            {
                "GoblinHeadframeList": [
                    self._protocol_frame(row) for row in self._frame_states(profile)
                ]
            },
        )

    def initial_snapshots(
        self, profile: Mapping[str, Any]
    ) -> Tuple[InitialSnapshot, ...]:
        return (
            InitialSnapshot(
                SC_GOBLIN_HEADFRAME_DATAS,
                self.make_goblin_headframe_datas(profile),
            ),
        )

    def _change_image_failure(
        self,
        profile: Mapping[str, Any],
        error_code: int,
        reason: str,
    ) -> ProfileCustomizationResult:
        return ProfileCustomizationResult(
            SC_CHANGE_IMAGE_RET,
            self.codec.encode(
                "SC_ChangeImageRet",
                {
                    "Errorcode": error_code,
                    "RoleId": _uint(profile.get("role_id"), maximum=UINT64_MAX),
                    "ImageId": _uint(
                        profile.get("head_image_id"),
                        DEFAULT_HEAD_IMAGE_ID,
                        UINT32_MAX,
                    ),
                },
            ),
            error_code=error_code,
            reason=reason,
        )

    def _change_image(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> ProfileCustomizationResult:
        try:
            fields = _varint_fields(request_body)
            if set(fields) - {1, 2}:
                raise ProfileCustomizationRequestError("CS_ChangeImage has unknown fields")
            requested_role = _single_uint(fields, 1, maximum=UINT64_MAX)
            image_id = _single_uint(fields, 2, maximum=UINT32_MAX)
        except ProfileCustomizationRequestError as exc:
            return self._change_image_failure(
                profile, ERROR_MESSAGE_DATA_ILLEGAL, str(exc)
            )
        role_id = _uint(profile.get("role_id"), maximum=UINT64_MAX)
        if requested_role not in {0, role_id}:
            return self._change_image_failure(
                profile,
                ERROR_CLIENT_REQUEST_PARAM,
                f"role {requested_role} is not the current role",
            )
        if image_id not in self._owned_avatar_ids(profile):
            return self._change_image_failure(
                profile,
                ERROR_CLIENT_REQUEST_PARAM,
                f"avatar {image_id} is not owned",
            )
        return ProfileCustomizationResult(
            SC_CHANGE_IMAGE_RET,
            self.codec.encode(
                "SC_ChangeImageRet",
                {"Errorcode": ERROR_SUCCESS, "RoleId": role_id, "ImageId": image_id},
            ),
            changes={"head_image_id": image_id},
        )

    def _frame_failure(
        self,
        request_id: int,
        error_code: int,
        reason: str,
        *,
        frame_id: int = 0,
        fake_level: int = 0,
    ) -> ProfileCustomizationResult:
        response_id = RESPONSE_IDS[request_id]
        if request_id == CS_USE_GOBLIN_HEADFRAME:
            body = self.codec.encode(
                "SC_UseGoblinHeadframeRet",
                {"Errorcode": error_code, "Id": frame_id},
            )
        else:
            body = self.codec.encode(
                "SC_SetGoblinHeadframeFakeLevelRet",
                {
                    "Errorcode": error_code,
                    "Id": frame_id,
                    "FakeLevel": fake_level,
                },
            )
        return ProfileCustomizationResult(
            response_id,
            body,
            error_code=error_code,
            reason=reason,
        )

    def _use_frame(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> ProfileCustomizationResult:
        try:
            fields = _varint_fields(request_body)
            if set(fields) - {1}:
                raise ProfileCustomizationRequestError(
                    "CS_UseGoblinHeadframe has unknown fields"
                )
            frame_id = _single_uint(fields, 1, maximum=UINT32_MAX)
        except ProfileCustomizationRequestError as exc:
            return self._frame_failure(
                CS_USE_GOBLIN_HEADFRAME,
                ERROR_MESSAGE_DATA_ILLEGAL,
                str(exc),
            )
        owned = set(self._owned_frame_ids_from_states(self._frame_states(profile)))
        if frame_id != 0 and frame_id not in owned:
            return self._frame_failure(
                CS_USE_GOBLIN_HEADFRAME,
                ERROR_CLIENT_REQUEST_PARAM,
                f"head frame {frame_id} is not owned",
                frame_id=frame_id,
            )
        return ProfileCustomizationResult(
            SC_USE_GOBLIN_HEADFRAME_RET,
            self.codec.encode(
                "SC_UseGoblinHeadframeRet",
                {"Errorcode": ERROR_SUCCESS, "Id": frame_id},
            ),
            changes={"head_frame_id": frame_id},
        )

    def _set_frame_fake_level(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> ProfileCustomizationResult:
        try:
            fields = _varint_fields(request_body)
            if set(fields) - {1, 2}:
                raise ProfileCustomizationRequestError(
                    "CS_SetGoblinHeadframeFakeLevel has unknown fields"
                )
            frame_id = _single_uint(fields, 1, maximum=UINT32_MAX)
            fake_level = _single_uint(fields, 2, maximum=UINT32_MAX)
        except ProfileCustomizationRequestError as exc:
            return self._frame_failure(
                CS_SET_GOBLIN_HEADFRAME_FAKE_LEVEL,
                ERROR_MESSAGE_DATA_ILLEGAL,
                str(exc),
            )
        config = self.frame_by_id.get(frame_id)
        states = self._frame_states(profile)
        owned = set(self._owned_frame_ids_from_states(states))
        if (
            config is None
            or frame_id not in owned
            or fake_level != int(config["lv"])
        ):
            return self._frame_failure(
                CS_SET_GOBLIN_HEADFRAME_FAKE_LEVEL,
                ERROR_CLIENT_REQUEST_PARAM,
                f"head frame {frame_id} level {fake_level} is not owned",
                frame_id=frame_id,
                fake_level=fake_level,
            )
        group_id = int(config["group"])
        updated = copy.deepcopy(states)
        changed_row: Optional[Dict[str, int]] = None
        for row in updated:
            if int(row["group_id"]) == group_id:
                row["fake_level"] = fake_level
                row["id"] = frame_id
                changed_row = row
                break
        if changed_row is None:  # Ownership validation makes this unreachable.
            return self._frame_failure(
                CS_SET_GOBLIN_HEADFRAME_FAKE_LEVEL,
                ERROR_CLIENT_REQUEST_PARAM,
                f"head frame group {group_id} is absent",
                frame_id=frame_id,
                fake_level=fake_level,
            )
        selected = _uint(profile.get("head_frame_id"), maximum=UINT32_MAX)
        notification = self.codec.encode(
            "Notify_UpdateGoblinHeadframe",
            {
                "GoblinHeadframeList": [self._protocol_frame(changed_row)],
                "RoleHeadframe": selected,
            },
        )
        return ProfileCustomizationResult(
            SC_SET_GOBLIN_HEADFRAME_FAKE_LEVEL_RET,
            self.codec.encode(
                "SC_SetGoblinHeadframeFakeLevelRet",
                {
                    "Errorcode": ERROR_SUCCESS,
                    "Id": frame_id,
                    "FakeLevel": fake_level,
                },
            ),
            changes={"goblin_headframes": updated},
            notifications=((NOTIFY_UPDATE_GOBLIN_HEADFRAME, notification),),
        )

    def handle(
        self,
        request_id: int,
        profile: Optional[Mapping[str, Any]] = None,
        request_body: bytes = b"",
    ) -> Optional[ProfileCustomizationResult]:
        profile = profile or {}
        if request_id == CS_REQUEST_HEAD_IMAGE_DATAS:
            if request_body:
                return ProfileCustomizationResult(
                    SC_HEAD_IMAGE_DATAS_RET,
                    self.codec.encode("SC_HeadImageDatasRet", {"HeadImageList": []}),
                    error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    reason="CS_RequestHeadImageDatas must have an empty body",
                )
            return ProfileCustomizationResult(
                SC_HEAD_IMAGE_DATAS_RET,
                self.make_head_image_datas_ret(profile),
            )
        if request_id == CS_CHANGE_IMAGE:
            return self._change_image(profile, request_body)
        if request_id == CS_USE_GOBLIN_HEADFRAME:
            return self._use_frame(profile, request_body)
        if request_id == CS_SET_GOBLIN_HEADFRAME_FAKE_LEVEL:
            return self._set_frame_fake_level(profile, request_body)
        return None

    @staticmethod
    def apply_changes(
        profile: MutableMapping[str, Any], changes: Mapping[str, Any]
    ) -> MutableMapping[str, Any]:
        profile.update(copy.deepcopy(dict(changes)))
        return profile


__all__ = [
    "CS_CHANGE_IMAGE",
    "CS_REQUEST_HEAD_IMAGE_DATAS",
    "CS_SET_GOBLIN_HEADFRAME_FAKE_LEVEL",
    "CS_USE_GOBLIN_HEADFRAME",
    "DEFAULT_HEAD_IMAGE_ID",
    "NOTIFY_UPDATE_GOBLIN_HEADFRAME",
    "PROFILE_CUSTOMIZATION_REVISION",
    "ProfileCustomizationLayer",
    "ProfileCustomizationResult",
    "RESPONSE_IDS",
    "SC_GOBLIN_HEADFRAME_DATAS",
]
