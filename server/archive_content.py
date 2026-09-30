"""Archive, collection, background, and secretary-board protocol state.

The source runtime keeps these features in one broad "archives" area even
though their messages are spread across several command ranges.  This module
provides one storage-agnostic layer for the defined offline server.  It uses
the validated :class:`~server.content_profile.ContentCatalog` as its sole ID
authority and :class:`~server.protobuf_codec.ProtocolMessageCodec` as its sole
response encoder.

``seed_archive_content`` expands the relatively small account-owned domains
into JSON-safe state.  Reapplying the seed is a value-level no-op and preserves
the mutable choices made through this layer (read words, current background,
and the three saved board pages).  ``ArchiveContentResult.changes`` is always a
top-level patch suitable for the server's atomic shallow merge::

    profile.update(result.changes)

The profile keys owned here are ``atlas_entries``, ``story_words``,
``music_box_ids``, ``sophisticate``, ``sophisticate_level``,
``unlocked_background_ids``, ``background_id``,
``unlocked_station_drawing_ids``, ``unlocked_teaching_guide_ids``,
``dress_collect``, ``dress_reward_ids``, ``board_items``, and
``cleared_red_dots``.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple, Union

try:  # Package imports in tests; fallback supports ``python server/server.py``.
    from .content_profile import ContentCatalog, default_content_catalog
    from .protobuf_codec import ProtocolMessageCodec
except ImportError:  # pragma: no cover - direct-script compatibility
    from content_profile import ContentCatalog, default_content_catalog  # type: ignore
    from protobuf_codec import ProtocolMessageCodec  # type: ignore


UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1

SC_ATLAS_DATAS = 805
NOTIFY_SOPHISTICATE_LEVEL_UP = 944

CS_REQUEST_MUSIC_BOX_DATAS = 566
SC_MUSIC_BOX_DATAS_RET = 567
CS_REQUEST_STORY_WORD_DATAS = 696
SC_REQUEST_STORY_WORD_DATAS_RET = 697
CS_READ_STORY_WORD = 698
SC_READ_STORY_WORD_RET = 699
CS_CLEAR_RED_DOT = 839
SC_CLEAR_RED_DOT_RET = 840
CS_SET_MAIN_BACKGROUND_IMAGE = 1320
SC_SET_MAIN_BACKGROUND_IMAGE_RET = 1321
CS_GET_MAIN_BACKGROUND_IMAGE_INFO = 1322
SC_GET_MAIN_BACKGROUND_IMAGE_INFO_RET = 1323
CS_REQUEST_DRESS_COLLECT = 1400
SC_REQUEST_DRESS_COLLECT_RET = 1401
CS_REQUEST_BOARD_PAGE_INFO = 1428
SC_REQUEST_BOARD_PAGE_INFO_RET = 1429
CS_SAVE_BOARD_PAGE_INFO = 1430
SC_SAVE_BOARD_PAGE_INFO_RET = 1431

INITIAL_RESPONSE_IDS = (
    SC_ATLAS_DATAS,
    NOTIFY_SOPHISTICATE_LEVEL_UP,
    SC_REQUEST_BOARD_PAGE_INFO_RET,
)

RESPONSE_IDS = {
    CS_REQUEST_MUSIC_BOX_DATAS: SC_MUSIC_BOX_DATAS_RET,
    CS_REQUEST_STORY_WORD_DATAS: SC_REQUEST_STORY_WORD_DATAS_RET,
    CS_READ_STORY_WORD: SC_READ_STORY_WORD_RET,
    CS_CLEAR_RED_DOT: SC_CLEAR_RED_DOT_RET,
    CS_SET_MAIN_BACKGROUND_IMAGE: SC_SET_MAIN_BACKGROUND_IMAGE_RET,
    CS_GET_MAIN_BACKGROUND_IMAGE_INFO: SC_GET_MAIN_BACKGROUND_IMAGE_INFO_RET,
    CS_REQUEST_DRESS_COLLECT: SC_REQUEST_DRESS_COLLECT_RET,
    CS_REQUEST_BOARD_PAGE_INFO: SC_REQUEST_BOARD_PAGE_INFO_RET,
    CS_SAVE_BOARD_PAGE_INFO: SC_SAVE_BOARD_PAGE_INFO_RET,
}
REQUEST_IDS = frozenset(RESPONSE_IDS)

# Msg.AtlasType, defined from runtime assembly.
ATLAS_MONSTER = 0
ATLAS_NPC = 1
ATLAS_CG = 2
ATLAS_HERO = 3
ATLAS_GOSPEL = 4

# DressCollectStatue, defined from the runtime.  ``SCORE`` means the item is
# owned and contributes its a_dress_collect.item_fraction to the collection.
DRESS_LOCKED = 0
DRESS_SCORE = 1
DRESS_NO_SCORE = 2
DRESS_EMPTY = 3

# Board page/model/skin values used by
# CmdManager.ConvertLocalData2ServerData and UserData.UpdateSecretaryInfo.
BOARD_MAIN = 1
BOARD_FIGHT = 2
BOARD_DAILY = 3
BOARD_TYPES = (BOARD_MAIN, BOARD_FIGHT, BOARD_DAILY)
MODEL_GOSPEL = 1
MODEL_HERO = 2
SKIN_GOSPEL_APPEARANCE = 1
SKIN_STATION_DRAWING = 2
SKIN_BASE_GOSPEL = 3
SKIN_BASE_HERO = 4

# Lillia is a complete hidden 1.0 hero defined by ``lilia_content``, but the
# shipped public ``a_hero`` table intentionally omits 3000000. Board requests
# may use that compatibility-only ID only when the account actually owns the
# corresponding hero row. Keeping this allowlist narrow prevents an arbitrary
# profile row from turning an unknown model ID into valid board state.
PROFILE_ONLY_HERO_IDS = frozenset({3_000_000})

# Msg.ErrorCode values used by the relevant runtime handlers.
ERROR_SUCCESS = 0
ERROR_STORY_WORD_ALREADY_READ = 327
ERROR_STORY_WORD_NOT_UNLOCKED = 328
ERROR_CLIENT_REQUEST_PARAM = 32000
ERROR_MESSAGE_DATA_ILLEGAL = 32769

SOPHISTICATE = 6_500
SOPHISTICATE_LEVEL = 60
ARCHIVE_CONTENT_REVISION = 1

DEFAULT_KANBAN_INFO = (
    "SecretaryPosInfo:(0.0, 0.0);"
    "SecretarySpinePosInfo:(0.0, 0.0);"
    "TriggerAreaPosInfo:(0.0, 0.0);"
    "SecretaryScaleInfo:1000;formatType:2"
)

# The verified 1.0 catalog is the compatibility floor.  Content overlays may
# append rows (for example a 2.0 gospel or skin), but must never silently drop
# any of the archive tables below the known-good 1.0 population.
EXPECTED_TABLE_COUNTS = {
    "a_people": 7,
    "a_gospel_skin": 44,
    "a_monster_basics": 136,
    "a_atlas_cg": 47,
    "a_atlas_npc": 12,
    "a_gospel": 75,
    "a_word": 45,
    "a_musicbox": 125,
    "a_background": 51,
    "a_station_drawing": 72,
    "a_dress_collect": 149,
    "a_dress_reward": 14,
    "a_teaching_guide": 138,
}

EXPECTED_UNLOCK_COUNTS = {
    "atlas": 216,
    "story_words": 45,
    "music": 83,
    "backgrounds": 51,
    "station_drawings": 72,
    "dresses": 149,
    "boards": 3,
}

REQUIRED_TABLE_FIELDS = {
    "a_people": {"id", "people_display"},
    "a_gospel_skin": {"id", "gospel_id"},
    "a_monster_basics": {"id", "atlas_unlock"},
    "a_atlas_cg": {"id"},
    "a_atlas_npc": {"id", "event_quantity", "npc_event"},
    "a_gospel": {"id", "using_gospel"},
    "a_word": {"id"},
    "a_musicbox": {"id", "music_hide", "music_picturename"},
    "a_background": {"id"},
    "a_station_drawing": {"id", "type", "people_id", "gospel_id", "skin_id"},
    "a_dress_collect": {"id", "dress_group", "item_fraction"},
    "a_dress_reward": {"id", "dress_group", "fraction_target"},
    "a_teaching_guide": {"id", "memory_number"},
}

EXPECTED_PROTOCOL_FIELDS = {
    "SC_AtlasDatas": {"AtlasList": 1},
    "Atlas": {"Type": 1, "AtlasId": 2, "CreateTime": 3, "Count": 4, "EventIdList": 5},
    "Notify_SophisticateLevelUp": {
        "RoleId": 1,
        "Sophisticatelevel": 3,
        "Sophisticate": 4,
    },
    "SC_MusicBoxDatasRet": {"MusicBoxIdList": 2},
    "SC_RequestStoryWordDatasRet": {"StoryWordList": 1},
    "StoryWord": {"WordId": 1, "Status": 2},
    "CS_ReadStoryWord": {"WordId": 1, "ReadAll": 2},
    "SC_ReadStoryWordRet": {"Errorcode": 1, "WordId": 2, "ReadAll": 3, "Status": 4},
    "CS_ClearRedDot": {"Type": 1, "Param1": 2},
    "SC_ClearRedDotRet": {"Errorcode": 1, "Type": 2, "Param1": 3},
    "CS_SetMainBackgroundImage": {"BackImageId": 1},
    "SC_SetMainBackgroundImageRet": {"Errorcode": 1, "BackImageId": 2},
    "SC_GetMainBackgroundImageInfoRet": {"Errorcode": 1, "UnlockImageIds": 2},
    "SC_RequestDressCollectRet": {
        "ErrorCode": 1,
        "DataList": 2,
        "MainDressId": 3,
        "DressRewardIds": 4,
    },
    "DressCollectItem": {"DressId": 1, "DressStatus": 2},
    "SC_RequestBoardPageInfoRet": {
        "Errorcode": 1,
        "BoardItems": 2,
        "UnlockDrawingIds": 3,
    },
    "BoardPageItem": {
        "BoardType": 1,
        "ModelType": 2,
        "ModelId": 3,
        "SkinType": 4,
        "SkinId": 5,
        "ShowType": 6,
        "KanbanInfo": 7,
    },
    "CS_SaveBoardPageInfo": {
        "BoardItemMain": 1,
        "BoardItemFight": 2,
        "BoardItemGod": 3,
    },
    "SC_SaveBoardPageInfoRet": {"Errorcode": 1},
}

_VECTOR_RE = re.compile(
    r"^\(\s*-?(?:\d+(?:\.\d*)?|\.\d+)\s*,\s*"
    r"-?(?:\d+(?:\.\d*)?|\.\d+)\s*\)$"
)


class ArchiveContentError(ValueError):
    """Raised when archive static data cannot form a safe runtime snapshot."""


class ArchiveRequestError(ValueError):
    """Raised when a protobuf request is malformed for this layer."""


@dataclass(frozen=True)
class AtlasRecord:
    atlas_type: int
    atlas_id: int
    create_time: int = 0
    count: int = 1
    event_ids: Tuple[int, ...] = ()

    def profile_value(self) -> Dict[str, Any]:
        return {
            "type": self.atlas_type,
            "id": self.atlas_id,
            "create_time": self.create_time,
            "count": self.count,
            "event_ids": list(self.event_ids),
        }

    def protocol_value(self) -> Dict[str, Any]:
        return {
            "Type": self.atlas_type,
            "AtlasId": self.atlas_id,
            "CreateTime": self.create_time,
            "Count": self.count,
            "EventIdList": list(self.event_ids),
        }


class InitialSnapshot(NamedTuple):
    """A tuple-compatible initial push with an explicit response-id alias."""

    command_id: int
    body: bytes

    @property
    def response_id(self) -> int:
        return self.command_id


@dataclass(frozen=True)
class ArchiveContentResult:
    """One request response and a pure, shallow-mergeable account patch."""

    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    reason: str = ""
    notifications: tuple = ()

    @property
    def command_id(self) -> int:
        return self.response_id

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


# Concise alias matching the other state-layer modules.
Result = ArchiveContentResult


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _uint(value: Any, default: int = 0, maximum: int = UINT32_MAX) -> int:
    parsed = _as_int(value, default)
    return parsed if 0 <= parsed <= maximum else default


def _mapping(value: Any) -> Mapping[Any, Any]:
    return value if isinstance(value, Mapping) else {}


def _lookup(mapping: Mapping[Any, Any], record_id: int, default: Any = None) -> Any:
    if record_id in mapping:
        return mapping[record_id]
    return mapping.get(str(record_id), default)


def _row_value(
    row: Mapping[str, Any], snake_name: str, wire_name: str, default: Any = 0
) -> Any:
    if snake_name in row:
        return row[snake_name]
    return row.get(wire_name, default)


def _read_varint(data: bytes, offset: int) -> Tuple[int, int]:
    value = 0
    shift = 0
    for _ in range(10):
        if offset >= len(data):
            raise ArchiveRequestError("truncated protobuf varint")
        current = data[offset]
        offset += 1
        value |= (current & 0x7F) << shift
        if not current & 0x80:
            return value, offset
        shift += 7
    raise ArchiveRequestError("protobuf varint exceeds 64 bits")


def _wire_fields(data: bytes) -> Iterable[Tuple[int, int, Any]]:
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if number <= 0:
            raise ArchiveRequestError("protobuf field zero is invalid")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ArchiveRequestError("truncated fixed64 field")
            value = data[offset:end]
            offset = end
        elif wire_type == 2:
            size, offset = _read_varint(data, offset)
            end = offset + size
            if end > len(data):
                raise ArchiveRequestError("truncated length-delimited field")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ArchiveRequestError("truncated fixed32 field")
            value = data[offset:end]
            offset = end
        else:
            raise ArchiveRequestError(f"unsupported protobuf wire type {wire_type}")
        yield number, wire_type, value


def _decode_message(
    codec: ProtocolMessageCodec,
    message_name: str,
    data: bytes,
) -> Dict[str, Any]:
    """Decode the small request subset using the same defined schema.

    Unknown protobuf fields remain forward-compatible and are ignored.  Known
    fields with the wrong wire type are rejected instead of being interpreted
    as an ID with an unsafe default.
    """

    schema = codec.schema(message_name)
    fields = {int(row["number"]): row for row in schema.get("fields", [])}
    result: Dict[str, Any] = {}
    for number, wire_type, raw in _wire_fields(bytes(data)):
        field_info = fields.get(number)
        if field_info is None:
            continue
        name = str(field_info["name"])
        type_info = field_info.get("type", {})
        kind = type_info.get("kind")

        if kind == "message":
            if wire_type != 2 or not isinstance(raw, bytes):
                raise ArchiveRequestError(f"{message_name}.{name} is not a message")
            result[name] = _decode_message(
                codec,
                str(type_info.get("type") or ""),
                raw,
            )
            continue

        if kind == "repeated":
            arguments = type_info.get("arguments") or []
            if len(arguments) != 1:
                raise ArchiveRequestError(f"{message_name}.{name} has no element type")
            element = arguments[0]
            values = result.setdefault(name, [])
            if element.get("kind") == "message":
                if wire_type != 2 or not isinstance(raw, bytes):
                    raise ArchiveRequestError(
                        f"{message_name}.{name} is not a message list"
                    )
                values.append(
                    _decode_message(codec, str(element.get("type") or ""), raw)
                )
            elif wire_type == 0 and isinstance(raw, int):
                values.append(raw)
            elif wire_type == 2 and isinstance(raw, bytes):
                packed_offset = 0
                while packed_offset < len(raw):
                    item, packed_offset = _read_varint(raw, packed_offset)
                    values.append(item)
            else:
                raise ArchiveRequestError(
                    f"{message_name}.{name} has the wrong wire type"
                )
            continue

        if kind not in {"scalar", "enum"}:
            raise ArchiveRequestError(f"{message_name}.{name} has unsupported schema")
        scalar = str(type_info.get("scalar") or "")
        if scalar in {"string", "bytes"}:
            if wire_type != 2 or not isinstance(raw, bytes):
                raise ArchiveRequestError(
                    f"{message_name}.{name} is not length-delimited"
                )
            if scalar == "string":
                try:
                    result[name] = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ArchiveRequestError(
                        f"{message_name}.{name} is not UTF-8"
                    ) from exc
            else:
                result[name] = raw
        else:
            if wire_type != 0 or not isinstance(raw, int):
                raise ArchiveRequestError(f"{message_name}.{name} is not a varint")
            result[name] = raw
    return result


def _story_status(value: Any) -> int:
    if isinstance(value, Mapping):
        value = value.get("status", value.get("Status", 0))
    return 1 if _as_int(value, 0) == 1 else 0


def _valid_kanban_info(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 1_024:
        return False
    pieces = value.split(";")
    if len(pieces) != 5:
        return False
    parsed: Dict[str, str] = {}
    for piece in pieces:
        if ":" not in piece:
            return False
        key, item = piece.split(":", 1)
        if key in parsed:
            return False
        parsed[key] = item.strip()
    vector_keys = {
        "SecretaryPosInfo",
        "SecretarySpinePosInfo",
        "TriggerAreaPosInfo",
    }
    if set(parsed) != vector_keys | {"SecretaryScaleInfo", "formatType"}:
        return False
    if any(not _VECTOR_RE.fullmatch(parsed[key]) for key in vector_keys):
        return False
    try:
        scale = int(parsed["SecretaryScaleInfo"])
        format_type = int(parsed["formatType"])
    except ValueError:
        return False
    return 0 < scale <= 100_000 and format_type in {0, 1, 2}


class ArchiveCatalog:
    """Feature-specific, validated projection of ``ContentCatalog``."""

    def __init__(self, catalog: ContentCatalog) -> None:
        self.content = catalog
        tables = {}
        for name, expected_count in EXPECTED_TABLE_COUNTS.items():
            table = catalog.table(name)
            missing = REQUIRED_TABLE_FIELDS[name] - set(table.fields)
            if missing:
                raise ArchiveContentError(
                    f"{name} is missing archive fields: {sorted(missing)}"
                )
            if table.count < expected_count:
                raise ArchiveContentError(
                    f"{name} has {table.count} records, expected at least "
                    f"{expected_count}"
                )
            tables[name] = table

        people = tables["a_people"]
        gospel = tables["a_gospel"]
        self.hero_ids = tuple(
            int(row["id"]) for row in people.records if row.get("people_display") == 1
        )
        self.gospel_ids = tuple(
            int(row["id"]) for row in gospel.records if row.get("using_gospel") == 1
        )
        self.hero_id_set = frozenset(self.hero_ids)
        self.gospel_id_set = frozenset(self.gospel_ids)
        self.gospel_skin_owner = {
            int(row["id"]): int(row["gospel_id"])
            for row in tables["a_gospel_skin"].records
        }
        if set(self.gospel_skin_owner.values()) - self.gospel_id_set:
            raise ArchiveContentError(
                "a_gospel_skin contains a board skin for an unavailable gospel"
            )

        atlas: List[AtlasRecord] = []
        atlas.extend(
            AtlasRecord(ATLAS_MONSTER, int(row["id"]))
            for row in tables["a_monster_basics"].records
            if row.get("atlas_unlock") == 1
        )
        for row in tables["a_atlas_npc"].records:
            atlas.append(
                AtlasRecord(
                    ATLAS_NPC,
                    int(row["id"]),
                    event_ids=self._npc_event_ids(row),
                )
            )
        atlas.extend(
            AtlasRecord(ATLAS_CG, record_id) for record_id in tables["a_atlas_cg"].ids
        )
        atlas.extend(AtlasRecord(ATLAS_HERO, record_id) for record_id in self.hero_ids)
        atlas.extend(
            AtlasRecord(ATLAS_GOSPEL, record_id) for record_id in self.gospel_ids
        )
        self.atlas_records = tuple(atlas)

        self.word_ids = tables["a_word"].ids
        self.word_id_set = frozenset(self.word_ids)
        self.music_ids = tuple(
            int(row["id"])
            for row in tables["a_musicbox"].records
            if row.get("music_hide") == 1
        )
        for record_id in self.music_ids:
            if not tables["a_musicbox"].record(record_id).get("music_picturename"):
                raise ArchiveContentError(
                    f"a_musicbox[{record_id}] is selectable but has no picture resource"
                )

        self.background_ids = tables["a_background"].ids
        self.background_id_set = frozenset(self.background_ids)
        self.default_background_id = self.background_ids[0]

        station = tables["a_station_drawing"]
        self.station_drawing_ids = station.ids
        self.station_drawing_id_set = frozenset(station.ids)
        self.station_drawing_by_id = station.by_id
        self._validate_station_drawings(station.records)

        dress = tables["a_dress_collect"]
        rewards = tables["a_dress_reward"]
        self.dress_ids = dress.ids
        self.dress_id_set = frozenset(dress.ids)
        self.dress_reward_ids = rewards.ids
        self.dress_reward_id_set = frozenset(rewards.ids)
        dress_groups = {int(row["dress_group"]) for row in dress.records}
        reward_groups = {int(row["dress_group"]) for row in rewards.records}
        if len(dress_groups) != 1 or reward_groups != dress_groups:
            raise ArchiveContentError(
                "a_dress_collect and a_dress_reward must share one dress_group"
            )
        self.main_dress_id = next(iter(dress_groups))

        self.teaching_guide_ids = tables["a_teaching_guide"].ids
        if any(
            int(row["memory_number"]) <= 0 for row in tables["a_teaching_guide"].records
        ):
            raise ArchiveContentError(
                "a_teaching_guide contains non-positive memory_number"
            )

        actual_unlock_counts = {
            "atlas": len(self.atlas_records),
            "story_words": len(self.word_ids),
            "music": len(self.music_ids),
            "backgrounds": len(self.background_ids),
            "station_drawings": len(self.station_drawing_ids),
            "dresses": len(self.dress_ids),
            "boards": len(BOARD_TYPES),
        }
        underfilled_unlocks = {
            name: (actual_unlock_counts[name], expected_count)
            for name, expected_count in EXPECTED_UNLOCK_COUNTS.items()
            if actual_unlock_counts[name] < expected_count
        }
        if underfilled_unlocks:
            raise ArchiveContentError(
                "archive unlock counts are below the verified 1.0 floor: "
                f"{underfilled_unlocks}"
            )

    @staticmethod
    def _npc_event_ids(row: Mapping[str, Any]) -> Tuple[int, ...]:
        raw = row.get("npc_event")
        if raw in (None, ""):
            events: Any = []
        elif isinstance(raw, str):
            try:
                events = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ArchiveContentError(
                    f"a_atlas_npc[{row.get('id')}].npc_event is invalid JSON"
                ) from exc
        else:
            events = raw
        if not isinstance(events, (list, tuple)):
            raise ArchiveContentError(
                f"a_atlas_npc[{row.get('id')}].npc_event is not a list"
            )
        event_ids = []
        for event in events:
            if not isinstance(event, Mapping):
                raise ArchiveContentError(
                    f"a_atlas_npc[{row.get('id')}].npc_event contains a non-object"
                )
            event_id = _uint(event.get("storypack"), 0)
            if not event_id or event_id in event_ids:
                raise ArchiveContentError(
                    f"a_atlas_npc[{row.get('id')}] contains an invalid NPC event"
                )
            event_ids.append(event_id)
        if _as_int(row.get("event_quantity"), -1) != len(event_ids):
            raise ArchiveContentError(
                f"a_atlas_npc[{row.get('id')}] event_quantity disagrees with npc_event"
            )
        return tuple(event_ids)

    def _validate_station_drawings(self, records: Iterable[Mapping[str, Any]]) -> None:
        for row in records:
            record_id = int(row["id"])
            drawing_type = _as_int(row.get("type"), -1)
            people_id = _uint(row.get("people_id"), 0)
            gospel_id = _uint(row.get("gospel_id"), 0)
            appearance_skin_id = _uint(row.get("skin_id"), 0)
            if drawing_type not in {0, 1}:
                raise ArchiveContentError(
                    f"a_station_drawing[{record_id}] has invalid type {drawing_type}"
                )
            if bool(people_id) == bool(gospel_id):
                raise ArchiveContentError(
                    f"a_station_drawing[{record_id}] must target one model domain"
                )
            if people_id and people_id not in self.hero_id_set:
                raise ArchiveContentError(
                    f"a_station_drawing[{record_id}] targets unavailable hero {people_id}"
                )
            if gospel_id and gospel_id not in self.gospel_id_set:
                raise ArchiveContentError(
                    f"a_station_drawing[{record_id}] targets unavailable gospel {gospel_id}"
                )
            if drawing_type == 0 and appearance_skin_id:
                raise ArchiveContentError(
                    f"a_station_drawing[{record_id}] base art unexpectedly has skin_id"
                )
            if drawing_type == 1 and (not gospel_id or not appearance_skin_id):
                raise ArchiveContentError(
                    f"a_station_drawing[{record_id}] skin art lacks gospel/skin linkage"
                )


class ArchiveContentLayer:
    """Pure protocol/state layer for archive-related account content."""

    def __init__(
        self,
        catalog: Optional[Union[ContentCatalog, str, Path]] = None,
        codec: Optional[ProtocolMessageCodec] = None,
    ) -> None:
        if catalog is None:
            content = default_content_catalog()
        elif isinstance(catalog, ContentCatalog):
            content = catalog
        else:
            content = ContentCatalog.load(catalog)
        self.content_catalog = content
        self.catalog = ArchiveCatalog(content)
        self.codec = codec or ProtocolMessageCodec()
        self._verify_protocol()
        self.default_board_items = self._make_default_board_items()

    def _verify_protocol(self) -> None:
        for message_name, expected in EXPECTED_PROTOCOL_FIELDS.items():
            schema = self.codec.schema(message_name)
            actual = {
                str(row["name"]): int(row["number"]) for row in schema.get("fields", [])
            }
            if actual != expected:
                raise ArchiveContentError(
                    f"{message_name} protocol fields disagree: {actual}"
                )

    def _make_default_board_items(self) -> Tuple[Dict[str, Any], ...]:
        # These are the exact safe fallbacks constructed by
        # UserData.GetShowModelInfo when the server has no saved board state.
        definitions = (
            (BOARD_MAIN, MODEL_HERO, 3_000_001, SKIN_BASE_HERO, 3_000_001),
            (BOARD_FIGHT, MODEL_GOSPEL, 3_013_017, SKIN_BASE_GOSPEL, 3_013_017),
            (BOARD_DAILY, MODEL_GOSPEL, 3_013_023, SKIN_BASE_GOSPEL, 3_013_023),
        )
        result = []
        for board_type, model_type, model_id, skin_type, skin_id in definitions:
            if model_type == MODEL_HERO and model_id not in self.catalog.hero_id_set:
                raise ArchiveContentError(
                    f"default board hero {model_id} is unavailable"
                )
            if (
                model_type == MODEL_GOSPEL
                and model_id not in self.catalog.gospel_id_set
            ):
                raise ArchiveContentError(
                    f"default board gospel {model_id} is unavailable"
                )
            result.append(
                {
                    "board_type": board_type,
                    "model_type": model_type,
                    "model_id": model_id,
                    "skin_type": skin_type,
                    "skin_id": skin_id,
                    "show_type": 0,
                    "kanban_info": DEFAULT_KANBAN_INFO,
                }
            )
        return tuple(result)

    def _normalize_board(
        self,
        value: Any,
        expected_type: Optional[int] = None,
        profile_hero_ids: Iterable[int] = (),
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(value, Mapping):
            return None
        board_type = _uint(_row_value(value, "board_type", "BoardType"), 0)
        model_type = _uint(_row_value(value, "model_type", "ModelType"), 0)
        model_id = _uint(_row_value(value, "model_id", "ModelId"), 0)
        skin_type = _uint(_row_value(value, "skin_type", "SkinType"), 0)
        skin_id = _uint(_row_value(value, "skin_id", "SkinId"), 0)
        show_type = _uint(_row_value(value, "show_type", "ShowType"), 0)
        kanban_info = _row_value(
            value,
            "kanban_info",
            "KanbanInfo",
            DEFAULT_KANBAN_INFO,
        )
        if expected_type is not None and board_type != expected_type:
            return None
        if board_type not in BOARD_TYPES or show_type not in {0, 1}:
            return None
        if not _valid_kanban_info(kanban_info):
            return None

        if skin_type == SKIN_GOSPEL_APPEARANCE:
            owner_id = self.catalog.gospel_skin_owner.get(skin_id)
            if (
                model_type != MODEL_GOSPEL
                or owner_id is None
                or model_id != owner_id
            ):
                return None
        elif skin_type == SKIN_STATION_DRAWING:
            drawing = self.catalog.station_drawing_by_id.get(skin_id)
            if drawing is None:
                return None
            people_id = _uint(drawing.get("people_id"), 0)
            gospel_id = _uint(drawing.get("gospel_id"), 0)
            if people_id:
                if model_type != MODEL_HERO or model_id != people_id:
                    return None
            elif model_type != MODEL_GOSPEL or model_id != gospel_id:
                return None
        elif skin_type == SKIN_BASE_HERO:
            if (
                model_type != MODEL_HERO
                or (
                    model_id not in self.catalog.hero_id_set
                    and model_id not in profile_hero_ids
                )
                or skin_id != model_id
            ):
                return None
        elif skin_type == SKIN_BASE_GOSPEL:
            if (
                model_type != MODEL_GOSPEL
                or model_id not in self.catalog.gospel_id_set
                or skin_id != model_id
            ):
                return None
        else:
            return None

        return {
            "board_type": board_type,
            "model_type": model_type,
            "model_id": model_id,
            "skin_type": skin_type,
            "skin_id": skin_id,
            "show_type": show_type,
            "kanban_info": str(kanban_info),
        }

    @staticmethod
    def _profile_only_hero_ids(profile: Mapping[str, Any]) -> frozenset[int]:
        rows = profile.get("heroes")
        if not isinstance(rows, Sequence) or isinstance(
            rows, (str, bytes, bytearray)
        ):
            return frozenset()
        return frozenset(
            hero_id
            for row in rows
            if isinstance(row, Mapping)
            for hero_id in (_uint(row.get("id"), 0),)
            if hero_id in PROFILE_ONLY_HERO_IDS
        )

    def _board_items(self, profile: Mapping[str, Any]) -> List[Dict[str, Any]]:
        by_type: Dict[int, Dict[str, Any]] = {}
        profile_hero_ids = self._profile_only_hero_ids(profile)
        raw = profile.get("board_items")
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            for value in raw:
                normalized = self._normalize_board(
                    value,
                    profile_hero_ids=profile_hero_ids,
                )
                if normalized is not None:
                    by_type.setdefault(normalized["board_type"], normalized)
        result = []
        for default in self.default_board_items:
            result.append(copy.deepcopy(by_type.get(default["board_type"], default)))
        return result

    def _story_words(self, profile: Mapping[str, Any]) -> Dict[str, Dict[str, int]]:
        current = _mapping(profile.get("story_words"))
        return {
            str(word_id): {"status": _story_status(_lookup(current, word_id, 0))}
            for word_id in self.catalog.word_ids
        }

    def _dress_state(self, profile: Mapping[str, Any]) -> Dict[str, Dict[str, int]]:
        del profile
        return {
            str(dress_id): {"status": DRESS_SCORE}
            for dress_id in self.catalog.dress_ids
        }

    def _current_background(self, profile: Mapping[str, Any]) -> int:
        current = _uint(
            profile.get("background_id"), self.catalog.default_background_id
        )
        if current not in self.catalog.background_id_set:
            return self.catalog.default_background_id
        return current

    @staticmethod
    def _cleared_red_dots(profile: Mapping[str, Any]) -> List[Dict[str, int]]:
        raw = profile.get("cleared_red_dots")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            return []
        result = []
        seen = set()
        for row in raw:
            if not isinstance(row, Mapping):
                continue
            dot_type = _as_int(row.get("type"), -1)
            param1 = _as_int(row.get("param1"), -1)
            key = (dot_type, param1)
            if (
                not 0 <= dot_type <= UINT32_MAX
                or not 0 <= param1 <= UINT32_MAX
                or key in seen
            ):
                continue
            seen.add(key)
            result.append({"type": dot_type, "param1": param1})
        return result

    def seed(self, profile: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        """Seed or migrate one profile in place without resetting choices."""

        if not isinstance(profile, MutableMapping):
            raise TypeError("profile must be a mutable mapping")
        if profile.get("player_progression"):
            owned = {int(r["id"]) for key in ("heroes", "gospels") for r in profile.get(key, [])}
            known = {(r["type"], r["id"]): copy.deepcopy(r) for r in profile.get("atlas_entries", [])}
            for record in self.catalog.atlas_records:
                if record.atlas_id in owned:
                    known.setdefault((record.atlas_type, record.atlas_id), record.profile_value())
            profile["atlas_entries"] = list(known.values())
            profile["story_words"] = self._story_words(profile)
            for key in ("music_box_ids", "unlocked_background_ids", "unlocked_station_drawing_ids",
                        "unlocked_teaching_guide_ids", "dress_reward_ids"):
                profile.setdefault(key, [])
            profile.setdefault("sophisticate", 0)
            profile.setdefault("sophisticate_level", 0)
            profile["background_id"] = self._current_background(profile)
            profile["dress_collect"] = self._dress_state(profile)
            profile["board_items"] = self._board_items(profile)
            profile["cleared_red_dots"] = self._cleared_red_dots(profile)
            profile["archive_content_revision"] = ARCHIVE_CONTENT_REVISION
            profile["archive_content_catalog_version"] = self.catalog.content.version
            return profile
        profile["atlas_entries"] = [
            record.profile_value() for record in self.catalog.atlas_records
        ]
        profile["story_words"] = self._story_words(profile)
        profile["music_box_ids"] = list(self.catalog.music_ids)
        profile["sophisticate"] = SOPHISTICATE
        profile["sophisticate_level"] = SOPHISTICATE_LEVEL
        profile["unlocked_background_ids"] = list(self.catalog.background_ids)
        profile["background_id"] = self._current_background(profile)
        profile["unlocked_station_drawing_ids"] = list(self.catalog.station_drawing_ids)
        profile["unlocked_teaching_guide_ids"] = list(self.catalog.teaching_guide_ids)
        profile["dress_collect"] = self._dress_state(profile)
        profile["dress_reward_ids"] = list(self.catalog.dress_reward_ids)
        profile["board_items"] = self._board_items(profile)
        profile["cleared_red_dots"] = self._cleared_red_dots(profile)
        profile["archive_content_revision"] = ARCHIVE_CONTENT_REVISION
        profile["archive_content_catalog_version"] = self.catalog.content.version
        return profile

    def seed_profile(
        self, profile: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        """Compatibility spelling used by the other persistent content layers."""

        return self.seed(profile)

    @staticmethod
    def _protocol_board(value: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "BoardType": int(value["board_type"]),
            "ModelType": int(value["model_type"]),
            "ModelId": int(value["model_id"]),
            "SkinType": int(value["skin_type"]),
            "SkinId": int(value["skin_id"]),
            "ShowType": int(value["show_type"]),
            "KanbanInfo": str(value["kanban_info"]),
        }

    def make_atlas_datas(self, profile: Optional[Mapping[str, Any]] = None) -> bytes:
        if profile and profile.get("player_progression"):
            return self.codec.encode("SC_AtlasDatas", {"AtlasList": [
                {"Type":r["type"],"AtlasId":r["id"],"CreateTime":r.get("create_time",0),
                 "Count":r.get("count",1),"EventIdList":r.get("event_ids",[])}
                for r in profile.get("atlas_entries",[])]})
        del profile  # Atlas ownership is the immutable all-unlocked baseline.
        return self.codec.encode(
            "SC_AtlasDatas",
            {"AtlasList": [row.protocol_value() for row in self.catalog.atlas_records]},
        )

    def make_sophisticate_level_up(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> bytes:
        profile = profile or {}
        role_id = _uint(profile.get("role_id"), 0, UINT64_MAX)
        sophisticate = _uint(profile.get("sophisticate"), SOPHISTICATE, UINT64_MAX)
        level = _uint(profile.get("sophisticate_level"), SOPHISTICATE_LEVEL)
        return self.codec.encode(
            "Notify_SophisticateLevelUp",
            {
                "RoleId": role_id,
                "Sophisticatelevel": level,
                "Sophisticate": sophisticate,
            },
        )

    def initial(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> Tuple[InitialSnapshot, InitialSnapshot, InitialSnapshot]:
        """Return archive state needed before the main interface is interactive."""

        return (
            InitialSnapshot(SC_ATLAS_DATAS, self.make_atlas_datas(profile)),
            InitialSnapshot(
                NOTIFY_SOPHISTICATE_LEVEL_UP,
                self.make_sophisticate_level_up(profile),
            ),
            # The main screen can render the saved secretary before opening
            # SecretaryView.  That view used to be the first caller of 1428,
            # leaving UserData.UnlockedStationDrawing empty during initial
            # rendering and making breakthrough-unlocked Gospel art appear
            # locked.  Its response handler is additive and safe as a login
            # snapshot, just like the adjacent archive packets.
            InitialSnapshot(
                SC_REQUEST_BOARD_PAGE_INFO_RET,
                self.make_board_page_info_ret(profile),
            ),
        )

    def initial_snapshots(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> Tuple[InitialSnapshot, InitialSnapshot, InitialSnapshot]:
        """Compatibility spelling for the server initialization dispatcher."""

        return self.initial(profile)

    def make_music_box_datas_ret(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> bytes:
        del profile
        return self.codec.encode(
            "SC_MusicBoxDatasRet",
            {"MusicBoxIdList": list(self.catalog.music_ids)},
        )

    def make_story_word_datas_ret(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> bytes:
        profile = profile or {}
        words = self._story_words(profile)
        return self.codec.encode(
            "SC_RequestStoryWordDatasRet",
            {
                "StoryWordList": [
                    {"WordId": word_id, "Status": words[str(word_id)]["status"]}
                    for word_id in self.catalog.word_ids
                ]
            },
        )

    def make_background_info_ret(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> bytes:
        del profile
        return self.codec.encode(
            "SC_GetMainBackgroundImageInfoRet",
            {
                "Errorcode": ERROR_SUCCESS,
                "UnlockImageIds": list(self.catalog.background_ids),
            },
        )

    def make_dress_collect_ret(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> bytes:
        profile = profile or {}
        dress_state = self._dress_state(profile)
        rewarded = profile.get("dress_reward_ids")
        if isinstance(rewarded, Sequence) and not isinstance(
            rewarded, (str, bytes, bytearray)
        ):
            rewarded_set = {
                _uint(value, 0)
                for value in rewarded
                if _uint(value, 0) in self.catalog.dress_reward_id_set
            }
            reward_ids = [
                value
                for value in self.catalog.dress_reward_ids
                if value in rewarded_set
            ]
        else:
            reward_ids = list(self.catalog.dress_reward_ids)
        return self.codec.encode(
            "SC_RequestDressCollectRet",
            {
                "ErrorCode": ERROR_SUCCESS,
                "DataList": [
                    {
                        "DressId": dress_id,
                        "DressStatus": dress_state[str(dress_id)]["status"],
                    }
                    for dress_id in self.catalog.dress_ids
                ],
                "MainDressId": self.catalog.main_dress_id,
                "DressRewardIds": reward_ids,
            },
        )

    def make_board_page_info_ret(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> bytes:
        profile = profile or {}
        return self.codec.encode(
            "SC_RequestBoardPageInfoRet",
            {
                "Errorcode": ERROR_SUCCESS,
                "BoardItems": [
                    self._protocol_board(row) for row in self._board_items(profile)
                ],
                "UnlockDrawingIds": list(self.catalog.station_drawing_ids),
            },
        )

    def _read_story_word(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> ArchiveContentResult:
        try:
            request = _decode_message(self.codec, "CS_ReadStoryWord", request_body)
            word_id = _uint(request.get("WordId"), 0)
            read_all = _uint(request.get("ReadAll"), 0)
            if read_all not in {0, 1}:
                raise ValueError("ReadAll must be zero or one")
        except ArchiveRequestError as exc:
            return ArchiveContentResult(
                SC_READ_STORY_WORD_RET,
                self.codec.encode(
                    "SC_ReadStoryWordRet",
                    {
                        "Errorcode": ERROR_MESSAGE_DATA_ILLEGAL,
                        "WordId": 0,
                        "ReadAll": 0,
                        "Status": 0,
                    },
                ),
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                reason=str(exc),
            )
        except ValueError as exc:
            return ArchiveContentResult(
                SC_READ_STORY_WORD_RET,
                self.codec.encode(
                    "SC_ReadStoryWordRet",
                    {
                        "Errorcode": ERROR_CLIENT_REQUEST_PARAM,
                        "WordId": word_id,
                        "ReadAll": 0,
                        "Status": 0,
                    },
                ),
                error_code=ERROR_CLIENT_REQUEST_PARAM,
                reason=str(exc),
            )

        words = self._story_words(profile)
        if read_all:
            unread = any(row["status"] == 0 for row in words.values())
            if not unread:
                error_code = ERROR_STORY_WORD_ALREADY_READ
                changes: Dict[str, Any] = {}
            else:
                for row in words.values():
                    row["status"] = 1
                error_code = ERROR_SUCCESS
                changes = {"story_words": words}
            status = 1
        elif word_id not in self.catalog.word_id_set:
            error_code = ERROR_STORY_WORD_NOT_UNLOCKED
            status = 0
            changes = {}
        elif words[str(word_id)]["status"] == 1:
            error_code = ERROR_STORY_WORD_ALREADY_READ
            status = 1
            changes = {}
        else:
            words[str(word_id)]["status"] = 1
            error_code = ERROR_SUCCESS
            status = 1
            changes = {"story_words": words}
        body = self.codec.encode(
            "SC_ReadStoryWordRet",
            {
                "Errorcode": error_code,
                "WordId": word_id,
                "ReadAll": read_all,
                "Status": status,
            },
        )
        return ArchiveContentResult(
            SC_READ_STORY_WORD_RET,
            body,
            changes=changes,
            error_code=error_code,
        )

    def _set_background(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> ArchiveContentResult:
        current = self._current_background(profile)
        try:
            request = _decode_message(
                self.codec, "CS_SetMainBackgroundImage", request_body
            )
            requested = _uint(request.get("BackImageId"), 0)
        except ArchiveRequestError as exc:
            body = self.codec.encode(
                "SC_SetMainBackgroundImageRet",
                {"Errorcode": ERROR_MESSAGE_DATA_ILLEGAL, "BackImageId": current},
            )
            return ArchiveContentResult(
                SC_SET_MAIN_BACKGROUND_IMAGE_RET,
                body,
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                reason=str(exc),
            )
        if requested in self.catalog.background_id_set:
            error_code = ERROR_SUCCESS
            selected = requested
            changes = {} if selected == current else {"background_id": selected}
        else:
            error_code = ERROR_CLIENT_REQUEST_PARAM
            selected = current
            changes = {}
        body = self.codec.encode(
            "SC_SetMainBackgroundImageRet",
            {"Errorcode": error_code, "BackImageId": selected},
        )
        return ArchiveContentResult(
            SC_SET_MAIN_BACKGROUND_IMAGE_RET,
            body,
            changes=changes,
            error_code=error_code,
        )

    def _save_board_pages(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> ArchiveContentResult:
        try:
            request = _decode_message(self.codec, "CS_SaveBoardPageInfo", request_body)
            names = ("BoardItemMain", "BoardItemFight", "BoardItemGod")
            boards = []
            profile_hero_ids = self._profile_only_hero_ids(profile)
            for board_type, name in zip(BOARD_TYPES, names):
                board = self._normalize_board(
                    request.get(name),
                    board_type,
                    profile_hero_ids,
                )
                if board is None:
                    raise ValueError(f"{name} violates board/model/skin invariants")
                boards.append(board)
        except ArchiveRequestError as exc:
            error_code = ERROR_MESSAGE_DATA_ILLEGAL
            reason = str(exc)
            changes: Dict[str, Any] = {}
        except ValueError as exc:
            error_code = ERROR_CLIENT_REQUEST_PARAM
            reason = str(exc)
            changes = {}
        else:
            error_code = ERROR_SUCCESS
            reason = ""
            changes = (
                {} if boards == self._board_items(profile) else {"board_items": boards}
            )
        return ArchiveContentResult(
            SC_SAVE_BOARD_PAGE_INFO_RET,
            self.codec.encode("SC_SaveBoardPageInfoRet", {"Errorcode": error_code}),
            changes=changes,
            error_code=error_code,
            reason=reason,
        )

    def _clear_red_dot(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> ArchiveContentResult:
        notices = ()
        try:
            request = _decode_message(self.codec, "CS_ClearRedDot", request_body)
            raw_type = request.get("Type", 0)
            raw_param1 = request.get("Param1", 0)
            if not isinstance(raw_type, int) or not 0 <= raw_type <= UINT32_MAX:
                raise ValueError("red-dot Type must be a uint32 value")
            if not isinstance(raw_param1, int) or not 0 <= raw_param1 <= UINT32_MAX:
                raise ValueError("red-dot fields must be uint32 values")
            dot_type = raw_type
            param1 = raw_param1
            if profile.get('player_progression') and dot_type == 2012 and (
                    param1 not in self.catalog.teaching_guide_ids or
                    param1 not in profile.get('unlocked_teaching_guide_ids', [])):
                raise ValueError('Teaching article is not unlocked')
        except ArchiveRequestError as exc:
            error_code = ERROR_MESSAGE_DATA_ILLEGAL
            dot_type = 0
            param1 = 0
            reason = str(exc)
            changes: Dict[str, Any] = {}
        except ValueError as exc:
            error_code = ERROR_CLIENT_REQUEST_PARAM
            dot_type = 0
            param1 = 0
            reason = str(exc)
            changes = {}
        else:
            error_code = ERROR_SUCCESS
            reason = ""
            rows = self._cleared_red_dots(profile)
            key = (dot_type, param1)
            if any((row["type"], row["param1"]) == key for row in rows):
                changes = {}
            else:
                rows.append({"type": dot_type, "param1": param1})
                changes = {"cleared_red_dots": rows}
            if profile.get('player_progression') and dot_type == 2012:
                read = set(profile.get('read_teaching_guide_ids', [])) | {param1}
                changes['read_teaching_guide_ids'] = sorted(read)
                notices = ((936, self.codec.encode('SC_TeachGuideDatas', {'GuideDatas': {
                    gid: int(gid in read) for gid in profile.get('unlocked_teaching_guide_ids', [])
                    if gid in self.catalog.teaching_guide_ids}})),)
        return ArchiveContentResult(
            SC_CLEAR_RED_DOT_RET,
            self.codec.encode(
                "SC_ClearRedDotRet",
                {"Errorcode": error_code, "Type": dot_type, "Param1": param1},
            ),
            changes=changes,
            error_code=error_code,
            reason=reason,
            notifications=notices,
        )

    def handle(
        self,
        request_id: int,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes = b"",
    ) -> Optional[ArchiveContentResult]:
        """Handle the nine archive requests or return ``None`` for another layer."""

        profile = profile or {}
        if request_id == CS_REQUEST_MUSIC_BOX_DATAS:
            response_body = self.make_music_box_datas_ret(profile)
        elif request_id == CS_REQUEST_STORY_WORD_DATAS:
            response_body = self.make_story_word_datas_ret(profile)
        elif request_id == CS_READ_STORY_WORD:
            return self._read_story_word(profile, request_body)
        elif request_id == CS_CLEAR_RED_DOT:
            return self._clear_red_dot(profile, request_body)
        elif request_id == CS_SET_MAIN_BACKGROUND_IMAGE:
            return self._set_background(profile, request_body)
        elif request_id == CS_GET_MAIN_BACKGROUND_IMAGE_INFO:
            response_body = self.make_background_info_ret(profile)
        elif request_id == CS_REQUEST_DRESS_COLLECT:
            response_body = self.make_dress_collect_ret(profile)
        elif request_id == CS_REQUEST_BOARD_PAGE_INFO:
            response_body = self.make_board_page_info_ret(profile)
        elif request_id == CS_SAVE_BOARD_PAGE_INFO:
            return self._save_board_pages(profile, request_body)
        else:
            return None
        return ArchiveContentResult(RESPONSE_IDS[request_id], response_body)

    @staticmethod
    def apply_changes(
        profile: MutableMapping[str, Any], changes: Mapping[str, Any]
    ) -> MutableMapping[str, Any]:
        """Apply a result patch without retaining aliases into the result."""

        profile.update(copy.deepcopy(dict(changes)))
        return profile


# Short alias for integration code that names every service implementation
# ``Layer``.
Layer = ArchiveContentLayer


@lru_cache(maxsize=1)
def default_layer() -> ArchiveContentLayer:
    return ArchiveContentLayer()


def seed_archive_content(
    profile: MutableMapping[str, Any],
    catalog: Optional[Union[ContentCatalog, str, Path]] = None,
) -> MutableMapping[str, Any]:
    layer = default_layer() if catalog is None else ArchiveContentLayer(catalog)
    return layer.seed(profile)


def initial_archive_content(
    profile: Optional[Mapping[str, Any]] = None,
) -> Tuple[InitialSnapshot, InitialSnapshot]:
    return default_layer().initial(profile)


def handle_archive_content_request(
    request_id: int,
    profile: Optional[Mapping[str, Any]],
    request_body: bytes = b"",
) -> Optional[ArchiveContentResult]:
    return default_layer().handle(request_id, profile, request_body)


__all__ = [
    "ARCHIVE_CONTENT_REVISION",
    "ATLAS_CG",
    "ATLAS_GOSPEL",
    "ATLAS_HERO",
    "ATLAS_MONSTER",
    "ATLAS_NPC",
    "ArchiveCatalog",
    "ArchiveContentError",
    "ArchiveContentLayer",
    "ArchiveContentResult",
    "BOARD_DAILY",
    "BOARD_FIGHT",
    "BOARD_MAIN",
    "BOARD_TYPES",
    "CS_CLEAR_RED_DOT",
    "CS_GET_MAIN_BACKGROUND_IMAGE_INFO",
    "CS_READ_STORY_WORD",
    "CS_REQUEST_BOARD_PAGE_INFO",
    "CS_REQUEST_DRESS_COLLECT",
    "CS_REQUEST_MUSIC_BOX_DATAS",
    "CS_REQUEST_STORY_WORD_DATAS",
    "CS_SAVE_BOARD_PAGE_INFO",
    "CS_SET_MAIN_BACKGROUND_IMAGE",
    "DEFAULT_KANBAN_INFO",
    "DRESS_EMPTY",
    "DRESS_LOCKED",
    "DRESS_NO_SCORE",
    "DRESS_SCORE",
    "ERROR_CLIENT_REQUEST_PARAM",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_STORY_WORD_ALREADY_READ",
    "ERROR_STORY_WORD_NOT_UNLOCKED",
    "ERROR_SUCCESS",
    "EXPECTED_PROTOCOL_FIELDS",
    "EXPECTED_TABLE_COUNTS",
    "EXPECTED_UNLOCK_COUNTS",
    "INITIAL_RESPONSE_IDS",
    "InitialSnapshot",
    "Layer",
    "MODEL_GOSPEL",
    "MODEL_HERO",
    "NOTIFY_SOPHISTICATE_LEVEL_UP",
    "REQUEST_IDS",
    "RESPONSE_IDS",
    "Result",
    "SC_ATLAS_DATAS",
    "SC_CLEAR_RED_DOT_RET",
    "SC_GET_MAIN_BACKGROUND_IMAGE_INFO_RET",
    "SC_MUSIC_BOX_DATAS_RET",
    "SC_READ_STORY_WORD_RET",
    "SC_REQUEST_BOARD_PAGE_INFO_RET",
    "SC_REQUEST_DRESS_COLLECT_RET",
    "SC_REQUEST_STORY_WORD_DATAS_RET",
    "SC_SAVE_BOARD_PAGE_INFO_RET",
    "SC_SET_MAIN_BACKGROUND_IMAGE_RET",
    "SKIN_BASE_GOSPEL",
    "SKIN_BASE_HERO",
    "SKIN_STATION_DRAWING",
    "SOPHISTICATE",
    "SOPHISTICATE_LEVEL",
    "default_layer",
    "handle_archive_content_request",
    "initial_archive_content",
    "seed_archive_content",
]
