"""Deterministic protobuf content snapshots and pure profile mutations.

The compatibility server intentionally writes protobuf by hand.  This module
follows that convention while keeping content/state handling independent from
``server.py`` and ``state.py``: callers pass a profile mapping in and receive
wire bytes plus, for stateful requests, the top-level values that should be
written back to the profile.

Static identifiers are loaded from ``generated/content``.  The one exception
is ``a_chapter_reward``: that table has not yet been materialized there, so its
60 IDs are pinned below from the verified TableConfig TextAsset instead of
being guessed from chapter IDs.  The wire layout is the one recorded in
``generated/protocol/protocol_catalog.json``; no generated protobuf runtime is
required.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, replace
from functools import lru_cache, wraps
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTENT_DIR = ROOT / "generated" / "content"

# Command IDs are taken from generated/protocol/protocol_catalog.json.
SC_CHAPTER_DATAS = 15
CS_REQUEST_GOSPEL_VOICE_DATA = 233
SC_GOSPEL_VOICE_DATA_RET = 234
CS_REQUEST_TASK_DATAS = 125
SC_TASK_DATAS_RET = 126
CS_TAKE_TASK_REWARD = 127
SC_TAKE_TASK_REWARD_RET = 128
CS_TAKE_ALL_TASK_REWARD = 140
SC_TAKE_ALL_TASK_REWARD_RET = 141
CS_SET_HERO_CUR_APPEARANCE_ID = 296
SC_SET_HERO_CUR_APPEARANCE_ID_RET = 297
CS_REQUEST_GOSPEL_APPEARANCE_DATAS = 298
SC_GOSPEL_APPEARANCE_DATAS_RET = 299
CS_SET_GOSPEL_CUR_APPEARANCE_ID = 302
SC_SET_GOSPEL_CUR_APPEARANCE_ID_RET = 303
CS_REQUEST_CARD_POOL_GROUP_DATAS = 368
SC_CARD_POOL_GROUP_DATAS_RET = 369
CS_REQUEST_ARCHITECTURE_DATAS = 391
SC_ARCHITECTURE_DATAS_RET = 392
CS_REQUEST_PUBLIC_GOSPEL_DATA = 393
SC_PUBLIC_GOSPEL_DATA_RET = 394
CS_LISTEN_GOSPEL_VOICE = 420
SC_LISTEN_GOSPEL_VOICE_RET = 421
CS_REQUEST_PLOT_DATAS = 428
SC_PLOT_DATAS_RET = 429
CS_READ_PLOT_OVER = 514
SC_READ_PLOT_OVER_RET = 515
CS_TAKE_PLOT_REWARD = 711
SC_TAKE_PLOT_REWARD_RET = 712
CS_REQUEST_GOSPEL_SKIN_DATA = 1106
SC_GOSPEL_SKIN_DATA_RET = 1107
CS_USE_GOSPEL_SKIN = 1109
SC_USE_GOSPEL_SKIN_RET = 1110
CS_REQUEST_GOSPEL_SKIN_STATUS = 1409
SC_REQUEST_GOSPEL_SKIN_STATUS_RET = 1410

# Msg enum values defined from runtime assembly.
DUNGEON_UNFINISHED = 0
DUNGEON_FINISHED = 1
TASK_UNFINISHED = 0
TASK_FINISHED = 1
TASK_REWARDED = 2
PLOT_ENTERED = 0
PLOT_FINISHED = 1
CONTENT_PROGRESS_COMPLETE = 100

# ``SC_ChapterDatas.RewardedChapterList`` is consumed by
# ``ChapterData.IsGotChapterReward(rewardId)``.  It therefore contains
# a_chapter_reward.id values, not a_chapte.id values.  The table is not part of
# the generated/content allowlist yet; these rows are kept as a small local
# compatibility projection rather than inferred from display text.
CHAPTER_REWARD_IDS_BY_CHAPTER: Mapping[int, Tuple[int, ...]] = {
    3930001: (5010101, 5010102, 5010103, 5010202, 5010203, 5010204),
    3930002: (5020101, 5020102, 5020103, 5020202, 5020203, 5020204),
    3930003: (5030101, 5030102, 5030103, 5030202, 5030203, 5030204),
    3930004: (5040101, 5040102, 5040103, 5040202, 5040203, 5040204),
    3930005: (5050102, 5050103, 5050104, 5050202, 5050203, 5050204),
    3930006: (5060101, 5060102, 5060103, 5060202, 5060203, 5060204),
    3930007: (5070101, 5070102, 5070103, 5070202, 5070203, 5070204),
    3930008: (5080101, 5080102, 5080103, 5080202, 5080203, 5080204),
    3930009: (5090101, 5090102, 5090103, 5090202, 5090203, 5090204),
    3930010: (5100101, 5100102, 5100103, 5100201, 5100202, 5100203),
}

ERROR_SUCCESS = 0
ERROR_NO_REWARD_CAN_BE_TAKEN = 122
ERROR_ILLEGAL_SKIN = 180
ERROR_ILLEGAL_DECORATION = 181
ERROR_GOSPEL_SKIN_NOT_FOUND = 416
ERROR_GOSPEL_NOT_FOUND = 424
ERROR_PLOT_NOT_UNLOCKED = 425
ERROR_HERO_NOT_FOUND = 516
ERROR_TASK_NOT_FOUND = 5503
ERROR_CONFIG_NOT_FOUND = 32704
ERROR_MESSAGE_DATA_ILLEGAL = 32769

TASK_CATEGORIES = ("main", "daily", "weekly", "activity")
TASK_WIRE_FIELDS = {"daily": 2, "weekly": 3, "main": 4}
TASK_CYCLE_CATEGORIES = {
    0: "daily",
    1: "weekly",
    2: "main",
    3: "activity",
    5: "activity",
}

# SC_TaskDatasRet has no ActivityTaskList field.  Activity tasks are retained
# in ``task_snapshots`` and mutation validation, but deliberately are not
# mislabeled as Msg.SC_TaskDatasRet.MazeTaskList.
UNSUPPORTED_TASK_WIRE_CATEGORIES = {
    "activity": "SC_TaskDatasRet has no activity-task field; activity data uses separate endpoints",
}

RESPONSE_IDS = {
    CS_REQUEST_GOSPEL_VOICE_DATA: SC_GOSPEL_VOICE_DATA_RET,
    CS_REQUEST_TASK_DATAS: SC_TASK_DATAS_RET,
    CS_TAKE_TASK_REWARD: SC_TAKE_TASK_REWARD_RET,
    CS_TAKE_ALL_TASK_REWARD: SC_TAKE_ALL_TASK_REWARD_RET,
    CS_SET_HERO_CUR_APPEARANCE_ID: SC_SET_HERO_CUR_APPEARANCE_ID_RET,
    CS_REQUEST_GOSPEL_APPEARANCE_DATAS: SC_GOSPEL_APPEARANCE_DATAS_RET,
    CS_SET_GOSPEL_CUR_APPEARANCE_ID: SC_SET_GOSPEL_CUR_APPEARANCE_ID_RET,
    CS_REQUEST_CARD_POOL_GROUP_DATAS: SC_CARD_POOL_GROUP_DATAS_RET,
    CS_REQUEST_ARCHITECTURE_DATAS: SC_ARCHITECTURE_DATAS_RET,
    CS_REQUEST_PUBLIC_GOSPEL_DATA: SC_PUBLIC_GOSPEL_DATA_RET,
    CS_LISTEN_GOSPEL_VOICE: SC_LISTEN_GOSPEL_VOICE_RET,
    CS_REQUEST_PLOT_DATAS: SC_PLOT_DATAS_RET,
    CS_READ_PLOT_OVER: SC_READ_PLOT_OVER_RET,
    CS_TAKE_PLOT_REWARD: SC_TAKE_PLOT_REWARD_RET,
    CS_REQUEST_GOSPEL_SKIN_DATA: SC_GOSPEL_SKIN_DATA_RET,
    CS_USE_GOSPEL_SKIN: SC_USE_GOSPEL_SKIN_RET,
    CS_REQUEST_GOSPEL_SKIN_STATUS: SC_REQUEST_GOSPEL_SKIN_STATUS_RET,
}

# These endpoints are recognized by the mutation API, but the local tables do
# not prove how every reward type should be granted.  Returning a failed,
# explicit result is safer than marking a reward claimed without granting it.
UNSUPPORTED_ENDPOINTS = {
    CS_TAKE_TASK_REWARD: "task reward grant/application is unsupported",
    CS_TAKE_ALL_TASK_REWARD: "bulk task reward grant/application is unsupported",
    CS_TAKE_PLOT_REWARD: "plot reward grant/application is unsupported",
}


class ProtobufDecodeError(ValueError):
    """Raised when a request is not valid for the protobuf subset used here."""


def encode_varint(value: int) -> bytes:
    """Encode an integer using protobuf's unsigned varint representation."""

    if value < 0:
        value &= (1 << 64) - 1
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def field_varint(number: int, value: int) -> bytes:
    return encode_varint(number << 3) + encode_varint(int(value))


def field_bytes(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def field_message(number: int, value: bytes) -> bytes:
    return field_bytes(number, value)


def field_string(number: int, value: str) -> bytes:
    return field_bytes(number, value.encode("utf-8"))


def field_packed_uint32(number: int, values: Iterable[int]) -> bytes:
    payload = b"".join(encode_varint(int(value)) for value in values)
    return field_bytes(number, payload) if payload else b""


def read_varint(data: bytes, offset: int = 0) -> Tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        current = data[offset]
        offset += 1
        value |= (current & 0x7F) << shift
        if not current & 0x80:
            return value, offset
        shift += 7
    raise ProtobufDecodeError("invalid or truncated protobuf varint")


def iter_wire_fields(data: bytes) -> Iterator[Tuple[int, int, object]]:
    """Yield ``(field_number, wire_type, value)`` for basic protobuf fields."""

    offset = 0
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if number == 0:
            raise ProtobufDecodeError("protobuf field number 0")
        if wire_type == 0:
            value, offset = read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ProtobufDecodeError("truncated fixed64 field")
            value = data[offset:end]
            offset = end
        elif wire_type == 2:
            size, offset = read_varint(data, offset)
            end = offset + size
            if end > len(data):
                raise ProtobufDecodeError("truncated length-delimited field")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ProtobufDecodeError("truncated fixed32 field")
            value = data[offset:end]
            offset = end
        else:
            raise ProtobufDecodeError(f"unsupported protobuf wire type {wire_type}")
        yield number, wire_type, value


def parse_packed_uint32_request(data: bytes, field_number: int = 1) -> Tuple[int, ...]:
    """Parse packed *and* unpacked repeated uint32 values from a request."""

    values: List[int] = []
    for number, wire_type, raw in iter_wire_fields(data):
        if number != field_number:
            continue
        if wire_type == 0:
            values.append(int(raw))
            continue
        if wire_type != 2 or not isinstance(raw, bytes):
            raise ProtobufDecodeError(
                f"field {field_number} must be packed or unpacked uint32"
            )
        offset = 0
        while offset < len(raw):
            value, offset = read_varint(raw, offset)
            values.append(value)
    if any(value < 0 or value > 0xFFFFFFFF for value in values):
        raise ProtobufDecodeError(f"field {field_number} contains a non-uint32 value")
    return tuple(values)


def _first_varint(data: bytes, field_number: int, default: int = 0) -> int:
    for number, wire_type, value in iter_wire_fields(data):
        if number == field_number:
            if wire_type != 0:
                raise ProtobufDecodeError(f"field {field_number} is not a varint")
            return int(value)
    return int(default)


def _first_message(data: bytes, field_number: int) -> bytes:
    for number, wire_type, value in iter_wire_fields(data):
        if number == field_number:
            if wire_type != 2 or not isinstance(value, bytes):
                raise ProtobufDecodeError(f"field {field_number} is not a message")
            return value
    return b""


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mapping(value: Any) -> Mapping[Any, Any]:
    return value if isinstance(value, Mapping) else {}


def _lookup(mapping: Mapping[Any, Any], key: int, default: Any = None) -> Any:
    if key in mapping:
        return mapping[key]
    return mapping.get(str(key), default)


def _valid_ids(values: Any, allowed: frozenset[int]) -> set[int]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    result = set()
    for value in values:
        parsed = _as_int(value, -1)
        if parsed in allowed:
            result.add(parsed)
    return result


def _full_catalog(profile: Mapping[str, Any]) -> bool:
    return bool(profile.get("all_content_unlocked")) or profile.get("profile_mode") in {
        "full",
        "all-unlocked",
        "test-all-unlocked",
    }


def _chapter_condition_ids(value: Any, *, chapter_id: int) -> Tuple[int, ...]:
    """Parse the achievement IDs consumed by UserData.CehckActiveConditions."""

    if value in (None, ""):
        return ()
    if not isinstance(value, str):
        raise ValueError(
            f"a_chapte[{chapter_id}].unlocking_conditions must be a JSON string"
        )
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"a_chapte[{chapter_id}].unlocking_conditions is not valid JSON"
        ) from exc
    if not isinstance(parsed, list) or any(
        type(condition_id) is not int or condition_id <= 0 for condition_id in parsed
    ):
        raise ValueError(
            f"a_chapte[{chapter_id}].unlocking_conditions must contain positive IDs"
        )
    return tuple(parsed)


def _install_activity_achievement_extension(
    content_dir: Path,
    achievement_ids: Sequence[int],
) -> None:
    """Extend the login-time achievement snapshot for all-unlocked profiles.

    The runtime does not consult ``Msg.Chapter.Progress`` for treasure,
    teaching, or hardest/challenge entry locks.  Those views resolve every
    ``a_chapte.unlocking_conditions`` value through UserData's achievement
    cache, populated by the 316 -> 317 login request.  The activity layer owns
    that response and is dispatched before this layer, so install a narrow
    post-processor on its existing builder instead of claiming the endpoint.

    Registration happens only when ContentResponseLayer is constructed (the
    all-unlocked server mode).  The wrapper returns the source result object
    for ordinary/minimal profiles, preserving their response byte-for-byte.
    """

    extension = b"".join(
        field_message(
            1,
            field_varint(1, achievement_id)
            + field_varint(4, 1)
            + field_varint(5, TASK_REWARDED),
        )
        for achievement_id in achievement_ids
    )
    if not extension:
        return

    try:  # Package imports in tests; fallback supports direct server execution.
        from . import activity_content
    except ImportError:  # pragma: no cover - ``python server/server.py`` compatibility
        import activity_content  # type: ignore

    layer_type = activity_content.ActivityContentLayer
    registry_name = "_offline_chapter_gate_achievement_extensions"
    wrapped_name = "_offline_chapter_gate_achievement_wrapper"
    registry = dict(getattr(layer_type, registry_name, {}))
    registry[Path(content_dir).resolve()] = extension
    setattr(layer_type, registry_name, registry)

    original = layer_type.request_achievement_datas
    if getattr(original, wrapped_name, False):
        return

    @wraps(original)
    def request_achievement_datas(activity_layer: Any, profile: Any) -> Any:
        result = original(activity_layer, profile)
        if not _full_catalog(profile or {}):
            return result
        extensions = getattr(type(activity_layer), registry_name, {})
        registered = extensions.get(Path(activity_layer.catalog.root).resolve(), b"")
        if not registered:
            return result
        return replace(result, body=result.body + registered)

    setattr(request_achievement_datas, wrapped_name, True)
    layer_type.request_achievement_datas = request_achievement_datas


def _task_status(value: Any) -> int:
    if isinstance(value, str):
        normalized = value.strip().lower().replace("_", "-")
        if normalized in {"finished", "completed", "complete"}:
            return TASK_FINISHED
        if normalized in {"rewarded", "claimed", "reward-claimed"}:
            return TASK_REWARDED
        return TASK_UNFINISHED
    status = _as_int(value, TASK_UNFINISHED)
    return status if status in {TASK_UNFINISHED, TASK_FINISHED, TASK_REWARDED} else TASK_UNFINISHED


@dataclass(frozen=True)
class TaskSnapshot:
    category: str
    task_id: int
    progress: int = 0
    status: int = TASK_UNFINISHED


@dataclass(frozen=True)
class ContentResult:
    """One response plus a pure, shallow-mergeable profile change set."""

    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    supported: bool = True
    reason: str = ""


class ContentCatalog:
    """Validated, immutable-enough view of the generated local content IDs."""

    def __init__(self, content_dir: Path = DEFAULT_CONTENT_DIR) -> None:
        self.content_dir = Path(content_dir).resolve()

        buildings = self._records("a_building")
        work_levels = self._records("a_work_level")
        task_main = self._records("a_task_main")
        task_daily = self._records("a_task_daily")
        task_weekly = self._records("a_task_weekly")
        task_activity = self._records("a_task_activity")
        storypacks = self._records("a_storypack")
        plot_reviews = self._records("a_plot_review")
        summons = self._records("a_summon")
        gospels = self._records("a_gospel")
        skins = self._records("a_gospel_skin")
        voices = self._optional_records("a_sound_voice")
        people = self._records("a_people")
        chapters = self._records("a_chapte")
        dungeons = self._records("a_dungeon")
        medals = self._records("a_medal")

        self.building_ids = tuple(sorted(_as_int(row["id"]) for row in buildings))
        maximums: Dict[int, int] = {building_id: 0 for building_id in self.building_ids}
        for row in work_levels:
            building_id = _as_int(row.get("building_id"))
            if building_id in maximums:
                maximums[building_id] = max(maximums[building_id], _as_int(row.get("work_level")))
        self.building_max_levels = maximums

        self.task_ids = {
            "main": tuple(sorted(_as_int(row["id"]) for row in task_main)),
            "daily": tuple(sorted(_as_int(row["id"]) for row in task_daily)),
            "weekly": tuple(sorted(_as_int(row["id"]) for row in task_weekly)),
            "activity": tuple(sorted(_as_int(row["id"]) for row in task_activity)),
        }
        self.task_id_sets = {
            category: frozenset(ids) for category, ids in self.task_ids.items()
        }

        # PlotReadData.PlotPkgId addresses a_storypack.  a_plot_review is a
        # strict archive/review subset, not the full legal PlotPkgId domain.
        self.plot_ids = tuple(sorted(_as_int(row["id"]) for row in storypacks))
        self.plot_id_set = frozenset(self.plot_ids)
        self.plot_review_ids = tuple(sorted(_as_int(row["id"]) for row in plot_reviews))

        # a_summon.id identifies the static configuration row only.  The wire
        # and persistence keys use two different columns: summonpool_group is
        # CardPoolData.CardPoolId, while strategy_group is the shared pity
        # CardPoolGroup.CardPoolGroupId.
        self.summon_rows = tuple(
            sorted(
                summons,
                key=lambda row: (
                    _as_int(row.get("summonpool_group")),
                    _as_int(row["id"]),
                ),
            )
        )
        self.summon_config_ids = tuple(
            sorted(_as_int(row["id"]) for row in self.summon_rows)
        )
        # Backwards-compatible catalog name: these remain configuration IDs,
        # never CardPoolData IDs or persistence keys.
        self.summon_ids = self.summon_config_ids
        self.summon_id_set = frozenset(self.summon_config_ids)
        self.card_pool_ids = tuple(
            _as_int(row.get("summonpool_group")) for row in self.summon_rows
        )
        self.card_pool_id_set = frozenset(self.card_pool_ids)
        self.card_pool_group_ids = tuple(
            sorted({_as_int(row.get("strategy_group")) for row in self.summon_rows})
        )
        self.card_pool_group_id_set = frozenset(self.card_pool_group_ids)
        if len(self.card_pool_id_set) != len(self.summon_rows):
            raise ValueError("a_summon contains duplicate summonpool_group pool IDs")

        # using_gospel=0 marks three effect/story-only records.  They remain in
        # the generated table but are never valid account-owned gospels.
        self.gospel_config_ids = tuple(sorted(_as_int(row["id"]) for row in gospels))
        self.gospel_ids = tuple(
            sorted(
                _as_int(row["id"])
                for row in gospels
                if _as_int(row.get("using_gospel"), 1) != 0
            )
        )
        self.gospel_id_set = frozenset(self.gospel_ids)
        self.unusable_gospel_ids = frozenset(self.gospel_config_ids) - self.gospel_id_set
        self.people_config_ids = tuple(sorted(_as_int(row["id"]) for row in people))
        self.people_ids = tuple(
            sorted(
                _as_int(row["id"])
                for row in people
                if _as_int(row.get("people_display"), 1) != 0
            )
        )
        self.people_id_set = frozenset(self.people_ids)
        self.unusable_people_ids = frozenset(self.people_config_ids) - self.people_id_set

        self.skin_rows = tuple(sorted(skins, key=lambda row: _as_int(row["id"])))
        self.skin_ids = tuple(_as_int(row["id"]) for row in self.skin_rows)
        self.skin_id_set = frozenset(self.skin_ids)
        self.skin_owner = {
            _as_int(row["id"]): _as_int(row.get("gospel_id")) for row in self.skin_rows
        }
        self.skin_model = {
            _as_int(row["id"]): (
                _as_int(row.get("model_id")) or _as_int(row.get("gospel_id"))
            )
            for row in self.skin_rows
        }
        skins_by_gospel: Dict[int, List[int]] = {}
        for skin_id, gospel_id in self.skin_owner.items():
            skins_by_gospel.setdefault(gospel_id, []).append(skin_id)
        self.skins_by_gospel = {
            gospel_id: tuple(sorted(ids)) for gospel_id, ids in skins_by_gospel.items()
        }
        self.skin_uses_own_voice = {
            _as_int(row["id"]): bool(_as_int(row.get("use_skinvoice")))
            for row in self.skin_rows
        }

        self.voice_rows = tuple(
            sorted(voices, key=lambda row: _as_int(row["id"]))
        )
        self.voice_by_id = {
            _as_int(row["id"]): row for row in self.voice_rows
        }
        voices_by_target: Dict[int, List[int]] = {}
        for row in self.voice_rows:
            if _as_int(row.get("target_type")) != 2:
                continue
            voices_by_target.setdefault(_as_int(row.get("target_id")), []).append(
                _as_int(row["id"])
            )
        self.voice_ids_by_target = {
            target_id: tuple(sorted(ids))
            for target_id, ids in voices_by_target.items()
        }

        self.chapter_rows = tuple(sorted(chapters, key=lambda row: _as_int(row["id"])))
        self.chapter_ids = tuple(_as_int(row["id"]) for row in self.chapter_rows)
        self.chapter_id_set = frozenset(self.chapter_ids)
        chapter_condition_ids = {
            condition_id
            for row in self.chapter_rows
            for condition_id in _chapter_condition_ids(
                row.get("unlocking_conditions"),
                chapter_id=_as_int(row["id"]),
            )
        }
        medal_achievement_ids = {
            _as_int(row.get("achieve_id"), -1) for row in medals
        }
        if any(achievement_id <= 0 for achievement_id in medal_achievement_ids):
            raise ValueError("a_medal contains an invalid achieve_id")
        # ActivityContentLayer already supplies every a_medal achievement.
        # Only append the disjoint closure needed by chapter entry conditions.
        self.chapter_gate_achievement_ids = tuple(
            sorted(chapter_condition_ids - medal_achievement_ids)
        )
        chapter_order = {chapter_id: index for index, chapter_id in enumerate(self.chapter_ids)}
        self.dungeon_rows = tuple(
            sorted(
                dungeons,
                key=lambda row: (
                    chapter_order.get(_as_int(row.get("belong_chapte")), len(chapter_order)),
                    _as_int(row.get("belong_chapte")),
                    _as_int(row.get("difficulty_type")),
                    _as_int(row.get("section")),
                    _as_int(row["id"]),
                ),
            )
        )
        self.dungeon_ids = tuple(_as_int(row["id"]) for row in self.dungeon_rows)
        self.dungeon_id_set = frozenset(self.dungeon_ids)
        self.dungeon_by_id = {_as_int(row["id"]): row for row in self.dungeon_rows}
        self.dungeon_max_stars = {
            _as_int(row["id"]): max(0, _as_int(row.get("star")))
            for row in self.dungeon_rows
        }
        self.dungeon_star_condition_ids = {
            _as_int(row["id"]): tuple(
                _as_int(value)
                for value in json.loads(row.get("star_level") or "[]")
                if _as_int(value) > 0
            )
            for row in self.dungeon_rows
        }

        self.chapter_reward_ids_by_chapter = {
            chapter_id: reward_ids
            for chapter_id, reward_ids in CHAPTER_REWARD_IDS_BY_CHAPTER.items()
            if chapter_id in self.chapter_id_set
        }
        self.chapter_reward_ids = tuple(
            reward_id
            for chapter_id in sorted(self.chapter_reward_ids_by_chapter)
            for reward_id in self.chapter_reward_ids_by_chapter[chapter_id]
        )
        self.chapter_reward_id_set = frozenset(self.chapter_reward_ids)

        starter_candidates = [
            row
            for row in self.dungeon_rows
            if _as_int(row.get("belong_chapte")) == self.chapter_ids[0]
            and _as_int(row.get("difficulty_type")) == 0
        ]
        self.starter_dungeon_id = min(
            starter_candidates,
            key=lambda row: (_as_int(row.get("section")), _as_int(row["id"])),
        )["id"]

    def _records(self, table_name: str) -> Tuple[Dict[str, Any], ...]:
        path = self.content_dir / f"{table_name}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get("records")
        ids = payload.get("ids")
        if not isinstance(records, list) or not isinstance(ids, list):
            raise ValueError(f"{path} does not contain generated records/ids")
        record_ids = [_as_int(row.get("id"), -1) for row in records if isinstance(row, dict)]
        if len(record_ids) != len(records) or record_ids != [_as_int(value, -1) for value in ids]:
            raise ValueError(f"{path} record IDs do not match its generated ID list")
        if _as_int(payload.get("count"), -1) != len(records) or len(set(record_ids)) != len(records):
            raise ValueError(f"{path} has an invalid generated row count or duplicate ID")
        return tuple(records)

    def _optional_records(self, table_name: str) -> Tuple[Dict[str, Any], ...]:
        """Load a newly generated extension table without breaking old roots."""

        if not (self.content_dir / f"{table_name}.json").is_file():
            return ()
        return self._records(table_name)


class ContentResponseLayer:
    """Build content responses and state transitions without owning storage."""

    def __init__(self, content_dir: Path = DEFAULT_CONTENT_DIR) -> None:
        self.catalog = ContentCatalog(content_dir)
        _install_activity_achievement_extension(
            self.catalog.content_dir,
            self.catalog.chapter_gate_achievement_ids,
        )

    def make_architecture_datas_ret(self, profile: Optional[Mapping[str, Any]] = None) -> bytes:
        profile = profile or {}
        saved = _mapping(profile.get("buildings"))
        payload = bytearray()
        for building_id in self.catalog.building_ids:
            value = _lookup(saved, building_id, 0)
            if isinstance(value, Mapping):
                value = value.get("level", 0)
            level = max(0, min(_as_int(value), self.catalog.building_max_levels[building_id]))
            architecture = field_varint(1, building_id) + field_varint(2, level)
            payload.extend(field_message(1, architecture))
        return bytes(payload)

    def _task_state(
        self,
        profile: Mapping[str, Any],
        category: str,
        task_id: int,
    ) -> Tuple[bool, int, int]:
        state: Dict[str, Any] = {}
        present = False

        def apply(value: Any) -> None:
            nonlocal present
            if value is marker:
                return
            present = True
            if isinstance(value, Mapping):
                state.update(value)
            else:
                state["status"] = value

        marker = object()
        tasks = _mapping(profile.get("tasks"))
        stage_tasks = _mapping(profile.get("stage_tasks"))
        apply(_lookup(tasks, task_id, marker))
        apply(_lookup(_mapping(tasks.get(category)), task_id, marker))
        apply(_lookup(stage_tasks, task_id, marker))
        apply(_lookup(_mapping(stage_tasks.get(category)), task_id, marker))

        table_name = f"a_task_{category}"
        content_state = _mapping(profile.get("content_state"))
        content_overrides = _mapping(content_state.get("overrides"))
        apply(_lookup(_mapping(content_overrides.get(table_name)), task_id, marker))
        if category == "activity":
            apply(
                _lookup(
                    _mapping(content_overrides.get("a_task_activity_daily")),
                    task_id,
                    marker,
                )
            )

        if not present:
            return False, 0, TASK_UNFINISHED
        progress = max(0, _as_int(state.get("progress")))
        status = _task_status(state.get("status", TASK_UNFINISHED))
        if bool(state.get("reward_claimed")):
            status = TASK_REWARDED
        return True, progress, status

    def task_snapshots(
        self,
        profile: Optional[Mapping[str, Any]] = None,
        *,
        include_all: Optional[bool] = None,
    ) -> Dict[str, Tuple[TaskSnapshot, ...]]:
        """Return all four validated task categories, including activity."""

        profile = profile or {}
        if include_all is None:
            include_all = _full_catalog(profile)
        result: Dict[str, Tuple[TaskSnapshot, ...]] = {}
        for category in TASK_CATEGORIES:
            rows = []
            for task_id in self.catalog.task_ids[category]:
                present, progress, status = self._task_state(profile, category, task_id)
                if include_all or present:
                    rows.append(TaskSnapshot(category, task_id, progress, status))
            result[category] = tuple(rows)
        return result

    @staticmethod
    def _encode_task(task: TaskSnapshot) -> bytes:
        return b"".join(
            (
                field_varint(1, task.task_id),
                field_varint(2, task.progress),
                field_varint(3, task.status),
            )
        )

    def make_task_datas_ret(self, profile: Optional[Mapping[str, Any]] = None) -> bytes:
        profile = profile or {}
        snapshots = self.task_snapshots(profile)
        payload = bytearray(field_varint(1, _as_int(profile.get("role_id"))))
        for category in ("daily", "weekly", "main"):
            field_number = TASK_WIRE_FIELDS[category]
            for task in snapshots[category]:
                payload.extend(field_message(field_number, self._encode_task(task)))
        payload.extend(field_varint(5, max(0, _as_int(profile.get("daily_refresh_remain_time")))))
        payload.extend(field_varint(6, max(0, _as_int(profile.get("weekly_refresh_remain_time")))))
        return bytes(payload)

    def task_state_changes(
        self,
        profile: Mapping[str, Any],
        category: str,
        task_id: int,
        *,
        progress: int,
        status: int,
    ) -> Dict[str, Any]:
        """Return a validated task patch for callers with proven progression.

        This helper does not grant rewards.  A caller may set ``Rewarded``
        only after its own reward layer has completed successfully.
        """

        if category not in self.catalog.task_id_sets or task_id not in self.catalog.task_id_sets[category]:
            raise ValueError(f"unknown {category!r} task ID {task_id}")
        if status not in {TASK_UNFINISHED, TASK_FINISHED, TASK_REWARDED}:
            raise ValueError(f"invalid task status {status}")
        tasks = copy.deepcopy(dict(_mapping(profile.get("tasks"))))
        category_tasks = dict(_mapping(tasks.get(category)))
        category_tasks[str(task_id)] = {
            "progress": max(0, _as_int(progress)),
            "status": status,
        }
        tasks[category] = category_tasks
        return {"tasks": tasks}

    @staticmethod
    def _encode_plot(plot_id: int, status: int, is_skip: int, is_rewarded: int) -> bytes:
        return b"".join(
            (
                field_varint(1, plot_id),
                field_varint(2, status),
                field_varint(3, is_skip),
                field_varint(4, is_rewarded),
            )
        )

    @staticmethod
    def _plot_status(value: Any) -> int:
        if isinstance(value, str):
            normalized = value.strip().lower().replace("_", "-")
            if normalized in {"finished", "completed", "complete"}:
                return PLOT_FINISHED
            return PLOT_ENTERED
        candidate = _as_int(value, PLOT_ENTERED)
        return candidate if candidate in {PLOT_ENTERED, PLOT_FINISHED} else PLOT_ENTERED

    def _plot_state(self, profile: Mapping[str, Any], plot_id: int) -> Tuple[bool, int, int, int]:
        """Resolve visibility, read status, skip, and claim as separate axes."""

        marker = object()
        # Full-catalog mode is a visibility baseline only.  Completion,
        # skipping, and reward claims are account events and start empty.
        present = _full_catalog(profile)
        status = PLOT_ENTERED
        is_skip = 0
        is_rewarded = 0

        def apply(value: Any, *, reward_only: bool = False) -> None:
            nonlocal present, status, is_skip, is_rewarded
            if value is marker:
                return
            present = True
            if isinstance(value, Mapping):
                if not reward_only and "status" in value:
                    status = max(status, self._plot_status(value.get("status")))
                if not reward_only and "is_skip" in value:
                    is_skip = max(is_skip, 1 if _as_int(value.get("is_skip")) else 0)
                for name in ("is_rewarded", "reward_claimed", "claimed"):
                    if name in value:
                        is_rewarded = max(
                            is_rewarded,
                            1 if _as_int(value.get(name)) else 0,
                        )
            elif reward_only:
                is_rewarded = max(is_rewarded, 1 if _as_int(value) else 0)
            else:
                status = max(status, self._plot_status(value))

        apply(_lookup(_mapping(profile.get("story_progress")), plot_id, marker))
        apply(_lookup(_mapping(profile.get("plots")), plot_id, marker))
        apply(
            _lookup(_mapping(profile.get("plot_rewards")), plot_id, marker),
            reward_only=True,
        )
        content_state = _mapping(profile.get("content_state"))
        content_overrides = _mapping(content_state.get("overrides"))
        apply(
            _lookup(_mapping(content_overrides.get("a_storypack")), plot_id, marker)
        )
        reward_history = profile.get("reward_history")
        if isinstance(reward_history, list):
            for entry in reward_history:
                if not isinstance(entry, Mapping) or entry.get("source") != "plot":
                    continue
                history_plot_id = _as_int(
                    entry.get("source_id", entry.get("plot_id")),
                    -1,
                )
                if history_plot_id == plot_id:
                    present = True
                    is_rewarded = 1
                    break
        canonical = _mapping(_lookup(_mapping(profile.get("plots")), plot_id, {}))
        if canonical.get("read_without_skip") is True:
            present, status, is_skip = True, PLOT_FINISHED, 0
        return present, status, is_skip, is_rewarded

    def make_plot_datas_ret(self, profile: Optional[Mapping[str, Any]] = None) -> bytes:
        profile = profile or {}
        include_all = _full_catalog(profile)
        payload = bytearray()
        for plot_id in self.catalog.plot_ids:
            present, status, is_skip, is_rewarded = self._plot_state(profile, plot_id)
            if include_all or present:
                payload.extend(
                    field_message(2, self._encode_plot(plot_id, status, is_skip, is_rewarded))
                )
        payload.extend(field_varint(4, max(0, _as_int(profile.get("activity_id")))))
        return bytes(payload)

    def make_card_pool_group_datas_ret(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> bytes:
        profile = profile or {}
        group_state = _mapping(profile.get("card_pool_groups"))
        pool_state = _mapping(profile.get("card_pools"))
        payload = bytearray()

        group_fields = (
            (4, "star5_fail_draw_num"),
            (5, "star4_fail_draw_num"),
            (6, "star5_prob"),
            (7, "star4_prob"),
            (8, "star5_not_up_num"),
            (9, "star4_not_up_num"),
            (10, "flare_fail_draw_num"),
            (11, "flare_prob"),
        )
        for group_id in self.catalog.card_pool_group_ids:
            state = _mapping(_lookup(group_state, group_id, {}))
            group = bytearray(field_varint(1, group_id))
            for number, name in group_fields:
                group.extend(field_varint(number, max(0, _as_int(state.get(name)))))
            payload.extend(field_message(1, bytes(group)))

        pool_fields = (
            (10, "star5_guaranteed_num"),
            (11, "star4_guaranteed_num"),
            (12, "draw_num"),
            (14, "ten_draw_num"),
        )
        for row in self.catalog.summon_rows:
            pool_id = _as_int(row.get("summonpool_group"))
            state = _mapping(_lookup(pool_state, pool_id, {}))
            pool = bytearray(
                field_varint(1, pool_id)
                + field_varint(2, max(0, _as_int(row.get("strategy_id"))))
            )
            for number, name in pool_fields:
                pool.extend(field_varint(number, max(0, _as_int(state.get(name)))))
            default_valid = 1 if _full_catalog(profile) else 0
            valid = 1 if _as_int(state.get("is_valid"), default_valid) else 0
            if _mapping(profile.get("player_progression")).get("mode") == "newcomer-v1":
                valid = 1
                if _as_int(row.get("open_type")) == 1 and row.get("open_condition"):
                    config, needed = map(int, str(row["open_condition"]).split(","))
                    prior = next((r for r in self.catalog.summon_rows if r["id"] == config), None)
                    actual = _mapping(_lookup(pool_state, prior["summonpool_group"], {})) if prior else {}
                    valid = int(prior is not None and _as_int(actual.get("draw_num")) >= needed)
                if row.get("highest_num") and _as_int(state.get("draw_num")) >= int(row["highest_num"]):
                    valid = 0
            pool.extend(field_varint(13, valid))
            payload.extend(field_message(2, bytes(pool)))
        return bytes(payload)

    @staticmethod
    def _entity_rows(profile: Mapping[str, Any], key: str) -> List[Mapping[str, Any]]:
        value = profile.get(key)
        if not isinstance(value, list):
            return []
        return [row for row in value if isinstance(row, Mapping)]

    @staticmethod
    def _appearance_container(
        profile: Mapping[str, Any], key: str, uid: int
    ) -> Tuple[Tuple[Mapping[str, Any], ...], int]:
        root = _mapping(profile.get(key))
        container = _lookup(root, uid, {})
        current_id = 0
        records: Sequence[Any] = ()
        if isinstance(container, Mapping):
            current_id = max(0, _as_int(container.get("current_id")))
            raw_records = container.get("appearances", container.get("appearance_list", ()))
            if isinstance(raw_records, (list, tuple)):
                records = raw_records
            elif all(str(item).isdigit() for item in container.keys()):
                records = [
                    dict(value, id=_as_int(item)) if isinstance(value, Mapping) else {"id": _as_int(item)}
                    for item, value in container.items()
                ]
        elif isinstance(container, (list, tuple)):
            records = container

        by_id: Dict[int, Mapping[str, Any]] = {0: {"id": 0}}
        for record in records:
            if not isinstance(record, Mapping):
                continue
            appearance_id = _as_int(record.get("id"), -1)
            if appearance_id >= 0:
                by_id[appearance_id] = record
        if current_id not in by_id:
            by_id[current_id] = {"id": current_id}
        return tuple(by_id[key] for key in sorted(by_id)), current_id

    @staticmethod
    def _encode_appearance(record: Mapping[str, Any], current_id: int) -> bytes:
        appearance_id = max(0, _as_int(record.get("id")))
        payload = bytearray(
            field_varint(1, appearance_id)
            + field_varint(3, 1 if appearance_id == current_id else 0)
        )
        name = record.get("name")
        if isinstance(name, str) and name:
            payload.extend(field_string(4, name[:64]))
        # DecorationList is intentionally not synthesized.  Saved decoration
        # endpoints are explicitly unsupported until their item/rune effects
        # can be rebuilt without losing state.
        return bytes(payload)

    def _make_appearance_datas_ret(
        self,
        profile: Mapping[str, Any],
        *,
        entity_key: str,
        appearance_key: str,
        valid_entity_ids: frozenset[int],
    ) -> bytes:
        rows = sorted(
            (
                row
                for row in self._entity_rows(profile, entity_key)
                if _as_int(row.get("id")) in valid_entity_ids
            ),
            key=lambda row: _as_int(row.get("uid")),
        )
        payload = bytearray()
        for row in rows:
            uid = _as_int(row.get("uid"))
            if uid <= 0:
                continue
            appearances, current_id = self._appearance_container(profile, appearance_key, uid)
            entity = bytearray(field_varint(1, uid))
            for appearance in appearances:
                entity.extend(field_message(2, self._encode_appearance(appearance, current_id)))
            payload.extend(field_message(1, bytes(entity)))
        return bytes(payload)

    def make_gospel_appearance_datas_ret(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> bytes:
        return self._make_appearance_datas_ret(
            profile or {},
            entity_key="gospels",
            appearance_key="gospel_appearances",
            valid_entity_ids=self.catalog.gospel_id_set,
        )

    @staticmethod
    def make_public_gospel_data_ret(
        profile: Optional[Mapping[str, Any]] = None,
    ) -> bytes:
        """Materialize ``SC_PublicGospelDataRet.PubGos`` even when empty.

        ``FriendNetMessageManager.GetMyShareGosplData`` dereferences
        ``msg.PubGos.GospelList`` without a null check.  An entirely empty
        protobuf therefore crashes the formation/public-gospel screen, while
        an explicitly encoded empty ``PublicGospel`` is the valid no-shares
        snapshot and gives the runtime a non-null repeated collection.
        """

        del profile
        return field_message(1, b"")

    def make_hero_appearance_datas_ret(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> bytes:
        return self._make_appearance_datas_ret(
            profile or {},
            entity_key="heroes",
            appearance_key="hero_appearances",
            valid_entity_ids=self.catalog.people_id_set,
        )

    def _owned_skin_ids(self, profile: Mapping[str, Any], gospel_uid: int = 0) -> set[int]:
        # Full profiles use a static ownership baseline instead of persisting
        # 44 redundant IDs.  Explicit sparse profile data is still merged in
        # so callers can retain state when moving between profile modes.
        ownership = {
            skin_id: _full_catalog(profile) for skin_id in self.catalog.skin_ids
        }

        def override(value: Any) -> Optional[bool]:
            if isinstance(value, Mapping):
                for key in ("owned", "unlocked", "status"):
                    if key in value:
                        return bool(_as_int(value.get(key)))
                return None
            if isinstance(value, (bool, int, str)):
                return bool(_as_int(value))
            return None

        def apply_mapping(values: Any) -> None:
            if not isinstance(values, Mapping):
                return
            for key, value in values.items():
                skin_id = _as_int(key, -1)
                if skin_id not in self.catalog.skin_id_set:
                    continue
                state = override(value)
                if state is not None:
                    ownership[skin_id] = state

        root = profile.get("gospel_skins")
        if isinstance(root, (list, tuple, set, frozenset)):
            for skin_id in _valid_ids(root, self.catalog.skin_id_set):
                ownership[skin_id] = True
        elif isinstance(root, Mapping):
            apply_mapping(root)
            if gospel_uid:
                container = _lookup(root, gospel_uid, ())
                if isinstance(container, Mapping):
                    nested = container.get(
                        "unlocked",
                        container.get("unlocked_skin_ids", container.get("skins", ())),
                    )
                    apply_mapping(container.get("statuses"))
                    container = nested
                for skin_id in _valid_ids(container, self.catalog.skin_id_set):
                    ownership[skin_id] = True

        if gospel_uid:
            for gospel in self._entity_rows(profile, "gospels"):
                if _as_int(gospel.get("uid")) != gospel_uid:
                    continue
                for name in ("unlocked_skin_ids", "skins"):
                    for skin_id in _valid_ids(gospel.get(name), self.catalog.skin_id_set):
                        ownership[skin_id] = True
                break

        # The status map and canonical content-state overrides are the most
        # specific sources and therefore apply after list-shaped legacy data.
        apply_mapping(profile.get("gospel_skin_status"))
        content_state = _mapping(profile.get("content_state"))
        content_overrides = _mapping(content_state.get("overrides"))
        apply_mapping(content_overrides.get("a_gospel_skin"))
        return {skin_id for skin_id, is_owned in ownership.items() if is_owned}

    def _gospel_for_uid(
        self, profile: Mapping[str, Any], gospel_uid: int
    ) -> Optional[Mapping[str, Any]]:
        return next(
            (
                row
                for row in self._entity_rows(profile, "gospels")
                if _as_int(row.get("uid")) == gospel_uid
                and _as_int(row.get("id")) in self.catalog.gospel_id_set
            ),
            None,
        )

    def _gospel_for_id(
        self, profile: Mapping[str, Any], gospel_id: int
    ) -> Optional[Mapping[str, Any]]:
        return next(
            (
                row
                for row in self._entity_rows(profile, "gospels")
                if _as_int(row.get("id")) == gospel_id
                and gospel_id in self.catalog.gospel_id_set
            ),
            None,
        )

    def _gospel_voice_targets(
        self,
        profile: Mapping[str, Any],
        gospel: Mapping[str, Any],
    ) -> frozenset[int]:
        gospel_id = _as_int(gospel.get("id"))
        gospel_uid = _as_int(gospel.get("uid"))
        owned_skins = self._owned_skin_ids(profile, gospel_uid)
        # A selected cosmetic remains a legal retained state even if a sparse
        # legacy profile omitted its redundant ownership list.
        for name in ("appearance_id", "appearance_id_2d"):
            skin_id = _as_int(gospel.get(name))
            if self.catalog.skin_owner.get(skin_id) == gospel_id:
                owned_skins.add(skin_id)

        targets = {gospel_id}
        targets.update(
            skin_id
            for skin_id in self.catalog.skins_by_gospel.get(gospel_id, ())
            if skin_id in owned_skins
            and self.catalog.skin_uses_own_voice.get(skin_id, False)
        )
        return frozenset(targets)

    def _configured_voice_ids_for_gospel(
        self,
        profile: Mapping[str, Any],
        gospel: Mapping[str, Any],
    ) -> frozenset[int]:
        return frozenset(
            voice_id
            for target_id in self._gospel_voice_targets(profile, gospel)
            for voice_id in self.catalog.voice_ids_by_target.get(target_id, ())
        )

    def _request_gospel_voice_data(
        self,
        profile: Mapping[str, Any],
        request_body: bytes,
    ) -> ContentResult:
        try:
            gospel_uid = _first_varint(request_body, 1)
        except ProtobufDecodeError as exc:
            return ContentResult(
                response_id=SC_GOSPEL_VOICE_DATA_RET,
                body=field_varint(1, 0),
                reason=str(exc),
            )

        gospel = self._gospel_for_uid(profile, gospel_uid) if gospel_uid else None
        unlocked: set[int] = set()
        reason = ""
        if gospel is None:
            reason = "gospel uid is zero or not owned"
        else:
            configured = self._configured_voice_ids_for_gospel(profile, gospel)
            raw_saved = gospel.get("unlocked_voice_ids", ())
            if isinstance(raw_saved, (list, tuple, set, frozenset)):
                unlocked.update(
                    voice_id
                    for voice_id in (_as_int(value, -1) for value in raw_saved)
                    if voice_id in configured
                )
            if _full_catalog(profile):
                unlocked.update(configured)
        return ContentResult(
            response_id=SC_GOSPEL_VOICE_DATA_RET,
            body=(
                field_varint(1, gospel_uid)
                + field_packed_uint32(2, sorted(unlocked))
            ),
            reason=reason,
        )

    def _listen_gospel_voice(
        self,
        profile: Mapping[str, Any],
        request_body: bytes,
    ) -> ContentResult:
        try:
            gospel_id = _first_varint(request_body, 1)
            voice_id = _first_varint(request_body, 2)
        except ProtobufDecodeError as exc:
            return self._error_result(
                SC_LISTEN_GOSPEL_VOICE_RET,
                ERROR_MESSAGE_DATA_ILLEGAL,
                reason=str(exc),
            )

        tail = field_varint(2, gospel_id) + field_varint(3, voice_id)
        if gospel_id <= 0 or voice_id <= 0:
            return self._error_result(
                SC_LISTEN_GOSPEL_VOICE_RET,
                ERROR_MESSAGE_DATA_ILLEGAL,
                tail,
                reason="gospel and voice IDs must be non-zero uint32 values",
            )
        gospel = self._gospel_for_id(profile, gospel_id)
        if gospel is None:
            return self._error_result(
                SC_LISTEN_GOSPEL_VOICE_RET,
                ERROR_GOSPEL_NOT_FOUND,
                tail,
                reason="gospel is not owned",
            )
        voice = self.catalog.voice_by_id.get(voice_id)
        if voice is None or _as_int(voice.get("target_type")) != 2:
            return self._error_result(
                SC_LISTEN_GOSPEL_VOICE_RET,
                ERROR_CONFIG_NOT_FOUND,
                tail,
                reason="voice configuration does not exist",
            )
        if _as_int(voice.get("target_id")) not in self._gospel_voice_targets(
            profile,
            gospel,
        ):
            return self._error_result(
                SC_LISTEN_GOSPEL_VOICE_RET,
                ERROR_MESSAGE_DATA_ILLEGAL,
                tail,
                reason="voice does not belong to the gospel or an owned voice skin",
            )
        # Listening has no durable side effect in the defined 1.0 runtime;
        # in particular it must not be confused with a voice-unlock event.
        return self._error_result(
            SC_LISTEN_GOSPEL_VOICE_RET,
            ERROR_SUCCESS,
            tail,
        )

    def make_gospel_skin_data_ret(
        self,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes,
    ) -> bytes:
        profile = profile or {}
        try:
            gospel_uid = _first_varint(request_body, 1)
        except ProtobufDecodeError:
            gospel_uid = 0
        gospel = self._gospel_for_uid(profile, gospel_uid)
        unlocked: Tuple[int, ...] = ()
        if gospel is not None:
            gospel_id = _as_int(gospel.get("id"))
            candidates = self.catalog.skins_by_gospel.get(gospel_id, ())
            owned = self._owned_skin_ids(profile, gospel_uid)
            unlocked = tuple(skin_id for skin_id in candidates if skin_id in owned)
        return field_varint(1, gospel_uid) + field_packed_uint32(2, unlocked)

    def make_gospel_skin_status_ret(
        self,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes,
    ) -> bytes:
        profile = profile or {}
        try:
            requested = tuple(sorted(set(parse_packed_uint32_request(request_body, 1))))
        except ProtobufDecodeError:
            return field_varint(1, ERROR_MESSAGE_DATA_ILLEGAL)
        if any(skin_id not in self.catalog.skin_id_set for skin_id in requested):
            return field_varint(1, ERROR_GOSPEL_SKIN_NOT_FOUND)

        owned = self._owned_skin_ids(profile)
        payload = bytearray(field_varint(1, ERROR_SUCCESS))
        for skin_id in requested:
            status = 1 if skin_id in owned else 0
            entry = field_varint(1, skin_id) + field_varint(2, status)
            payload.extend(field_message(2, entry))
        return bytes(payload)

    def _dungeon_star(
        self,
        profile: Mapping[str, Any],
        dungeon_id: int,
        *,
        default: int = 0,
    ) -> int:
        stars = _mapping(profile.get("dungeon_stars"))
        value = _lookup(stars, dungeon_id, None)
        if value is None:
            battle = _lookup(_mapping(profile.get("completed_battles")), dungeon_id, {})
            if isinstance(battle, Mapping) and "star" in battle:
                value = battle.get("star")
        maximum = self.catalog.dungeon_max_stars[dungeon_id]
        return max(0, min(_as_int(value, default), maximum))

    def _dungeon_achieved_conditions(
        self,
        profile: Mapping[str, Any],
        dungeon_id: int,
        *,
        all_unlocked: bool,
    ) -> Tuple[int, ...]:
        configured = self.catalog.dungeon_star_condition_ids[dungeon_id]
        saved = _lookup(
            _mapping(profile.get("dungeon_achieved_conditions")),
            dungeon_id,
            None,
        )
        if saved is None:
            battle = _lookup(
                _mapping(profile.get("completed_battles")),
                dungeon_id,
                {},
            )
            if isinstance(battle, Mapping):
                saved = battle.get("achieved_conditions")
        if saved is None and all_unlocked:
            return configured
        values = {
            _as_int(value)
            for value in saved
        } if isinstance(saved, (list, tuple)) else set()
        return tuple(value for value in configured if value in values)

    @staticmethod
    def _progress_percent(value: Any, default: int) -> int:
        return max(0, min(_as_int(value, default), CONTENT_PROGRESS_COMPLETE))

    @staticmethod
    def _reward_claimed(value: Any) -> bool:
        if isinstance(value, Mapping):
            for key in ("is_rewarded", "reward_claimed", "claimed", "status"):
                if key in value:
                    return bool(_as_int(value.get(key)))
            return False
        return bool(_as_int(value))

    def _rewarded_chapter_ids(
        self,
        profile: Mapping[str, Any],
        *,
        all_unlocked: bool,
    ) -> Tuple[int, ...]:
        """Resolve real a_chapter_reward IDs with legacy chapter-ID support."""

        rewarded = set(self.catalog.chapter_reward_ids if all_unlocked else ())
        saved = profile.get("chapter_rewards")
        if isinstance(saved, Mapping):
            chapter_decisions: List[Tuple[Tuple[int, ...], bool]] = []
            reward_decisions: List[Tuple[Tuple[int, ...], bool]] = []
            for raw_id, value in saved.items():
                saved_id = _as_int(raw_id, -1)
                if saved_id in self.catalog.chapter_reward_id_set:
                    reward_decisions.append(
                        ((saved_id,), self._reward_claimed(value))
                    )
                else:
                    targets = self.catalog.chapter_reward_ids_by_chapter.get(saved_id, ())
                    chapter_decisions.append((targets, self._reward_claimed(value)))
            # A reward-specific decision is more precise than the legacy
            # chapter-wide alias and therefore wins regardless of JSON order.
            for targets, claimed in chapter_decisions + reward_decisions:
                if claimed:
                    rewarded.update(targets)
                elif all_unlocked:
                    rewarded.difference_update(targets)
        elif isinstance(saved, (list, tuple, set, frozenset)):
            # A legacy sequence is an authoritative claimed set.  It may hold
            # either real reward IDs or the old server's chapter IDs.
            rewarded.clear()
            for raw_id in saved:
                saved_id = _as_int(raw_id, -1)
                if saved_id in self.catalog.chapter_reward_id_set:
                    rewarded.add(saved_id)
                else:
                    rewarded.update(
                        self.catalog.chapter_reward_ids_by_chapter.get(saved_id, ())
                    )
        return tuple(sorted(rewarded & self.catalog.chapter_reward_id_set))

    def _chapter_ids_for_minimal(self, dungeon_ids: Iterable[int]) -> Tuple[int, ...]:
        result = {
            _as_int(self.catalog.dungeon_by_id[dungeon_id].get("belong_chapte"))
            for dungeon_id in dungeon_ids
        }
        return tuple(sorted(result & self.catalog.chapter_id_set))

    def make_chapter_datas(
        self,
        profile: Optional[Mapping[str, Any]] = None,
        *,
        full_catalog: Optional[bool] = None,
    ) -> bytes:
        """Build SC_ChapterDatas without conflating visibility and progress.

        A full catalog contains every generated a_dungeon row (2246 in the
        bundled catalog).  For an all-unlocked profile this is a completed,
        replayable snapshot; forcing ``full_catalog=True`` for an old profile
        still changes visibility only.
        """

        profile = profile or {}
        if full_catalog is None:
            full_catalog = _full_catalog(profile)
        # The normal economy keeps content visible, but stars, clear flags
        # and claimed chapter rewards must come from actual saved play.
        all_unlocked = _full_catalog(profile) and _mapping(profile.get("offline_economy")).get("mode") != "normal-v1"
        completed = _valid_ids(profile.get("completed_dungeons"), self.catalog.dungeon_id_set)
        if all_unlocked:
            completed = set(self.catalog.dungeon_ids)

        if full_catalog:
            dungeon_ids = self.catalog.dungeon_ids
            chapter_ids = self.catalog.chapter_ids
        else:
            selected = set(completed)
            for key in ("unlocked_dungeons", "selected_dungeon_ids"):
                selected.update(_valid_ids(profile.get(key), self.catalog.dungeon_id_set))
            last_dungeon_id = _as_int(profile.get("last_dungeon_id"), -1)
            if last_dungeon_id in self.catalog.dungeon_id_set:
                selected.add(last_dungeon_id)
            if not selected:
                selected.add(_as_int(self.catalog.starter_dungeon_id))
            dungeon_ids = tuple(
                dungeon_id for dungeon_id in self.catalog.dungeon_ids if dungeon_id in selected
            )
            chapter_ids = self._chapter_ids_for_minimal(dungeon_ids)

        payload = bytearray()
        for dungeon_id in dungeon_ids:
            state = DUNGEON_FINISHED if dungeon_id in completed else DUNGEON_UNFINISHED
            star_default = self.catalog.dungeon_max_stars[dungeon_id] if all_unlocked else 0
            dungeon = b"".join(
                (
                    field_varint(1, dungeon_id),
                    field_varint(2, state),
                    field_varint(
                        3,
                        self._dungeon_star(
                            profile,
                            dungeon_id,
                            default=star_default,
                        ),
                    ),
                    field_packed_uint32(
                        4,
                        self._dungeon_achieved_conditions(
                            profile,
                            dungeon_id,
                            all_unlocked=all_unlocked,
                        ),
                    ),
                )
            )
            payload.extend(field_message(1, dungeon))

        payload.extend(
            field_packed_uint32(
                2,
                self._rewarded_chapter_ids(profile, all_unlocked=all_unlocked),
            )
        )

        difficulties = _mapping(profile.get("chapter_difficulties"))
        progress = _mapping(profile.get("chapter_progress"))
        followed = _as_int(profile.get("followed_chapter_id"))
        progress_default = CONTENT_PROGRESS_COMPLETE if all_unlocked else 0
        for chapter_id in chapter_ids:
            chapter = b"".join(
                (
                    field_varint(1, chapter_id),
                    field_varint(2, max(0, _as_int(_lookup(difficulties, chapter_id, 0)))),
                    field_varint(3, 0),
                    field_varint(
                        4,
                        self._progress_percent(
                            _lookup(progress, chapter_id, progress_default),
                            progress_default,
                        ),
                    ),
                    field_varint(5, 1 if chapter_id == followed else 0),
                )
            )
            payload.extend(field_message(3, chapter))
        payload.extend(
            field_varint(
                4,
                self._progress_percent(
                    profile.get("overall_progress"),
                    progress_default,
                ),
            )
        )
        payload.extend(field_varint(5, max(0, _as_int(profile.get("fast_battle_daily_num")))))
        return bytes(payload)

    def make_full_chapter_datas(self, profile: Optional[Mapping[str, Any]] = None) -> bytes:
        return self.make_chapter_datas(profile, full_catalog=True)

    @staticmethod
    def _error_result(
        response_id: int,
        error_code: int,
        body_tail: bytes = b"",
        *,
        reason: str = "",
        supported: bool = True,
    ) -> ContentResult:
        return ContentResult(
            response_id=response_id,
            body=field_varint(1, error_code) + body_tail,
            changes={},
            supported=supported,
            reason=reason,
        )

    def _set_current_appearance(
        self,
        profile: Mapping[str, Any],
        request_body: bytes,
        *,
        entity_key: str,
        appearance_key: str,
        response_id: int,
    ) -> ContentResult:
        try:
            uid = _first_varint(request_body, 1)
            appearance_id = _first_varint(request_body, 2)
        except ProtobufDecodeError as exc:
            return self._error_result(
                response_id,
                ERROR_MESSAGE_DATA_ILLEGAL,
                reason=str(exc),
            )

        valid_entity_ids = (
            self.catalog.people_id_set
            if entity_key == "heroes"
            else self.catalog.gospel_id_set
        )
        entity = next(
            (
                row
                for row in self._entity_rows(profile, entity_key)
                if _as_int(row.get("uid")) == uid
                and _as_int(row.get("id")) in valid_entity_ids
            ),
            None,
        )
        missing_error = ERROR_HERO_NOT_FOUND if entity_key == "heroes" else ERROR_GOSPEL_NOT_FOUND
        tail = field_varint(2, uid) + field_varint(3, appearance_id)
        if entity is None:
            return self._error_result(response_id, missing_error, tail)

        appearances, _ = self._appearance_container(profile, appearance_key, uid)
        allowed = {max(0, _as_int(row.get("id"))) for row in appearances}
        # AppearanceId selects a saved decoration preset.  Hero/gospel config
        # IDs and a_gospel_skin IDs belong to different protocol domains;
        # model skins are changed exclusively by CS_UseGospelSkin.
        if appearance_id not in allowed:
            return self._error_result(response_id, ERROR_ILLEGAL_SKIN, tail)

        root = copy.deepcopy(dict(_mapping(profile.get(appearance_key))))
        old_container = _lookup(root, uid, {})
        if isinstance(old_container, Mapping):
            container = copy.deepcopy(dict(old_container))
            raw_records = container.get("appearances", container.get("appearance_list", []))
            records = copy.deepcopy(raw_records) if isinstance(raw_records, list) else []
        elif isinstance(old_container, list):
            records = copy.deepcopy(old_container)
            container = {}
        else:
            records = []
            container = {}
        existing_ids = {
            _as_int(record.get("id"), -1)
            for record in records
            if isinstance(record, Mapping)
        }
        if 0 not in existing_ids:
            records.append({"id": 0})
        if appearance_id not in existing_ids and appearance_id != 0:
            records.append({"id": appearance_id})
        records.sort(key=lambda record: _as_int(record.get("id")) if isinstance(record, Mapping) else 0)
        container["current_id"] = appearance_id
        container["appearances"] = records
        root[str(uid)] = container
        return ContentResult(
            response_id=response_id,
            body=field_varint(1, ERROR_SUCCESS) + tail,
            changes={appearance_key: root},
        )

    def _use_gospel_skin(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> ContentResult:
        try:
            gospel_uid = _first_varint(request_body, 1)
            requested_3d = _first_varint(request_body, 2)
            requested_2d = _first_varint(request_body, 3)
        except ProtobufDecodeError as exc:
            return self._error_result(
                SC_USE_GOSPEL_SKIN_RET,
                ERROR_MESSAGE_DATA_ILLEGAL,
                reason=str(exc),
            )

        gospels = [
            row
            for row in self._entity_rows(profile, "gospels")
            if _as_int(row.get("id")) in self.catalog.gospel_id_set
        ]
        index = next(
            (index for index, row in enumerate(gospels) if _as_int(row.get("uid")) == gospel_uid),
            -1,
        )
        if index < 0:
            tail = (
                field_varint(2, gospel_uid)
                + field_varint(3, requested_3d)
                + field_varint(4, requested_2d)
            )
            return self._error_result(SC_USE_GOSPEL_SKIN_RET, ERROR_GOSPEL_NOT_FOUND, tail)

        gospel = gospels[index]
        gospel_id = _as_int(gospel.get("id"))
        current_3d = _as_int(gospel.get("appearance_id"), gospel_id) or gospel_id
        current_2d = _as_int(gospel.get("appearance_id_2d"), current_3d) or gospel_id
        raw_skin_3d = requested_3d or current_3d
        skin_2d = requested_2d or current_2d
        owned = self._owned_skin_ids(profile, gospel_uid)

        def allowed(skin_id: int, current: int) -> bool:
            if skin_id == gospel_id:
                return gospel_id in self.catalog.gospel_id_set
            if self.catalog.skin_owner.get(skin_id) != gospel_id:
                return False
            return skin_id in owned or skin_id == current

        if not allowed(raw_skin_3d, current_3d) or not allowed(skin_2d, current_2d):
            tail = (
                field_varint(2, gospel_uid)
                + field_varint(3, raw_skin_3d)
                + field_varint(4, skin_2d)
            )
            return self._error_result(SC_USE_GOSPEL_SKIN_RET, ERROR_ILLEGAL_SKIN, tail)

        # ``skin_type == 1`` cosmetics carry a 2D painting but deliberately
        # point ``model_id`` back to their owning Gospel.  The 1.0 runtime hides
        # those rows from its 3D-only picker; persisting the cosmetic ID in
        # AppearanceId nevertheless makes later 3D transitions enter the
        # skin-specific transform path before falling back to the base model.
        # Canonicalize that state to the real model ID while retaining the
        # cosmetic in AppearanceId2D.  Independent model skins keep their own
        # ID, so this also works for mixed 2D/3D selections.
        skin_3d = raw_skin_3d
        if (
            raw_skin_3d != gospel_id
            and self.catalog.skin_owner.get(raw_skin_3d) == gospel_id
            and self.catalog.skin_model.get(raw_skin_3d, gospel_id) == gospel_id
        ):
            skin_3d = gospel_id
        tail = (
            field_varint(2, gospel_uid)
            + field_varint(3, skin_3d)
            + field_varint(4, skin_2d)
        )

        updated_gospels = copy.deepcopy(list(profile.get("gospels", [])))
        # ``gospels`` can contain malformed legacy entries; locate the real
        # list position again instead of assuming the filtered index matches.
        for row in updated_gospels:
            if isinstance(row, dict) and _as_int(row.get("uid")) == gospel_uid:
                row["appearance_id"] = skin_3d
                row["appearance_id_2d"] = skin_2d
                break
        return ContentResult(
            response_id=SC_USE_GOSPEL_SKIN_RET,
            body=field_varint(1, ERROR_SUCCESS) + tail,
            changes={"gospels": updated_gospels},
        )

    def _read_plot_over(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> ContentResult:
        try:
            read_data = _first_message(request_body, 1)
            plot_id = _first_varint(read_data, 1)
            requested_status = _first_varint(read_data, 2, PLOT_ENTERED)
            is_skip = 1 if _first_varint(read_data, 3) else 0
            requested_rewarded = 1 if _first_varint(read_data, 4) else 0
            is_sophisticate = _first_varint(request_body, 2)
            activity_id = _first_varint(request_body, 3)
            sub_activity_id = _first_varint(request_body, 4)
        except ProtobufDecodeError as exc:
            return self._error_result(
                SC_READ_PLOT_OVER_RET,
                ERROR_MESSAGE_DATA_ILLEGAL,
                reason=str(exc),
            )
        if not read_data or requested_status not in {PLOT_ENTERED, PLOT_FINISHED}:
            return self._error_result(
                SC_READ_PLOT_OVER_RET,
                ERROR_MESSAGE_DATA_ILLEGAL,
                reason="ReadData/status is missing or invalid",
            )
        if plot_id not in self.catalog.plot_id_set:
            nested = self._encode_plot(plot_id, requested_status, is_skip, 0)
            return self._error_result(
                SC_READ_PLOT_OVER_RET,
                ERROR_PLOT_NOT_UNLOCKED,
                field_message(2, nested),
            )

        _, previous_status, previous_skip, previous_rewarded = self._plot_state(
            profile, plot_id
        )
        effective_status = max(previous_status, requested_status)
        # Read completion never grants a review reward.  Preserve an already
        # persisted claim in its source authority and ignore both the
        # runtime-supplied optimistic bit and catalog visibility defaults.
        read_without_skip = (previous_status == PLOT_FINISHED and not previous_skip) or (
            requested_status == PLOT_FINISHED and not is_skip)
        effective_skip = 0 if read_without_skip else max(previous_skip, is_skip)
        plots = copy.deepcopy(dict(_mapping(profile.get("plots"))))
        previous_plot = _lookup(plots, plot_id, {})
        plot_state = (
            copy.deepcopy(dict(previous_plot))
            if isinstance(previous_plot, Mapping)
            else {}
        )
        # Only the two facts proved by CS_ReadPlotOver are written here.
        # Existing claim/custom fields survive because this row is merged,
        # while an absent claim is never materialized as a synthetic default.
        plot_state["status"] = effective_status
        plot_state["is_skip"] = effective_skip
        if read_without_skip:
            plot_state["read_without_skip"] = True
        if previous_skip or is_skip:
            plot_state["ever_skipped"] = True
        plots.pop(plot_id, None)
        plots[str(plot_id)] = plot_state
        story_progress = copy.deepcopy(dict(_mapping(profile.get("story_progress"))))
        story_progress.pop(plot_id, None)
        story_progress[str(plot_id)] = effective_status

        nested = self._encode_plot(
            plot_id,
            effective_status,
            effective_skip,
            previous_rewarded,
        )
        body = b"".join(
            (
                field_varint(1, ERROR_SUCCESS),
                field_message(2, nested),
                field_varint(3, is_sophisticate),
                field_varint(5, activity_id),
                field_varint(6, sub_activity_id),
            )
        )
        reason = "client reward flag ignored; reward claiming is a separate validated transaction" if requested_rewarded else ""
        return ContentResult(
            response_id=SC_READ_PLOT_OVER_RET,
            body=body,
            changes={"plots": plots, "story_progress": story_progress},
            reason=reason,
        )

    def _unsupported_reward(
        self, request_id: int, profile: Mapping[str, Any], request_body: bytes
    ) -> ContentResult:
        reason = UNSUPPORTED_ENDPOINTS[request_id]
        try:
            if request_id == CS_TAKE_TASK_REWARD:
                role_id = _first_varint(request_body, 1, _as_int(profile.get("role_id")))
                task_id = _first_varint(request_body, 2)
                cycle = _first_varint(request_body, 3)
                category = TASK_CYCLE_CATEGORIES.get(cycle)
                error = (
                    ERROR_TASK_NOT_FOUND
                    if category is None or task_id not in self.catalog.task_id_sets[category]
                    else ERROR_NO_REWARD_CAN_BE_TAKEN
                )
                tail = (
                    field_varint(2, role_id)
                    + field_varint(3, task_id)
                    + field_varint(4, cycle)
                    + field_varint(6, _first_varint(request_body, 4))
                    + field_varint(8, _first_varint(request_body, 5))
                    + field_varint(9, _first_varint(request_body, 6))
                )
            elif request_id == CS_TAKE_ALL_TASK_REWARD:
                role_id = _first_varint(request_body, 1, _as_int(profile.get("role_id")))
                cycle = _first_varint(request_body, 2)
                error = ERROR_NO_REWARD_CAN_BE_TAKEN
                tail = (
                    field_varint(2, role_id)
                    + field_varint(3, cycle)
                    + field_varint(7, _first_varint(request_body, 3))
                    + field_varint(8, _first_varint(request_body, 4))
                    + field_varint(9, _first_varint(request_body, 5))
                )
            else:
                plot_id = _first_varint(request_body, 1)
                error = ERROR_NO_REWARD_CAN_BE_TAKEN
                tail = field_varint(2, plot_id) + field_varint(3, 0)
        except ProtobufDecodeError as exc:
            return self._error_result(
                RESPONSE_IDS[request_id],
                ERROR_MESSAGE_DATA_ILLEGAL,
                supported=False,
                reason=f"{reason}: {exc}",
            )
        return self._error_result(
            RESPONSE_IDS[request_id],
            error,
            tail,
            supported=False,
            reason=reason,
        )

    def handle(
        self,
        request_id: int,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes = b"",
    ) -> Optional[ContentResult]:
        """Return a response/mutation result, or ``None`` for another layer."""

        profile = profile or {}
        if request_id == CS_REQUEST_ARCHITECTURE_DATAS:
            body = self.make_architecture_datas_ret(profile)
        elif request_id == CS_REQUEST_GOSPEL_VOICE_DATA:
            return self._request_gospel_voice_data(profile, request_body)
        elif request_id == CS_LISTEN_GOSPEL_VOICE:
            return self._listen_gospel_voice(profile, request_body)
        elif request_id == CS_REQUEST_TASK_DATAS:
            body = self.make_task_datas_ret(profile)
        elif request_id == CS_REQUEST_PLOT_DATAS:
            body = self.make_plot_datas_ret(profile)
        elif request_id == CS_REQUEST_CARD_POOL_GROUP_DATAS:
            body = self.make_card_pool_group_datas_ret(profile)
        elif request_id == CS_REQUEST_GOSPEL_APPEARANCE_DATAS:
            body = self.make_gospel_appearance_datas_ret(profile)
        elif request_id == CS_REQUEST_PUBLIC_GOSPEL_DATA:
            body = self.make_public_gospel_data_ret(profile)
        elif request_id == CS_REQUEST_GOSPEL_SKIN_DATA:
            body = self.make_gospel_skin_data_ret(profile, request_body)
        elif request_id == CS_REQUEST_GOSPEL_SKIN_STATUS:
            body = self.make_gospel_skin_status_ret(profile, request_body)
        elif request_id == CS_SET_HERO_CUR_APPEARANCE_ID:
            return self._set_current_appearance(
                profile,
                request_body,
                entity_key="heroes",
                appearance_key="hero_appearances",
                response_id=SC_SET_HERO_CUR_APPEARANCE_ID_RET,
            )
        elif request_id == CS_SET_GOSPEL_CUR_APPEARANCE_ID:
            return self._set_current_appearance(
                profile,
                request_body,
                entity_key="gospels",
                appearance_key="gospel_appearances",
                response_id=SC_SET_GOSPEL_CUR_APPEARANCE_ID_RET,
            )
        elif request_id == CS_USE_GOSPEL_SKIN:
            return self._use_gospel_skin(profile, request_body)
        elif request_id == CS_READ_PLOT_OVER:
            return self._read_plot_over(profile, request_body)
        elif request_id in UNSUPPORTED_ENDPOINTS:
            return self._unsupported_reward(request_id, profile, request_body)
        else:
            return None
        return ContentResult(response_id=RESPONSE_IDS[request_id], body=body)


@lru_cache(maxsize=1)
def default_layer() -> ContentResponseLayer:
    return ContentResponseLayer()


# Small function wrappers keep server.py integration terse and preserve the
# existing ``make_*`` naming style used by its hand-written response builders.
def make_architecture_datas_ret(profile: Optional[Mapping[str, Any]] = None) -> bytes:
    return default_layer().make_architecture_datas_ret(profile)


def make_task_datas_ret(profile: Optional[Mapping[str, Any]] = None) -> bytes:
    return default_layer().make_task_datas_ret(profile)


def make_plot_datas_ret(profile: Optional[Mapping[str, Any]] = None) -> bytes:
    return default_layer().make_plot_datas_ret(profile)


def make_card_pool_group_datas_ret(profile: Optional[Mapping[str, Any]] = None) -> bytes:
    return default_layer().make_card_pool_group_datas_ret(profile)


def make_gospel_appearance_datas_ret(profile: Optional[Mapping[str, Any]] = None) -> bytes:
    return default_layer().make_gospel_appearance_datas_ret(profile)


def make_public_gospel_data_ret(
    profile: Optional[Mapping[str, Any]] = None,
) -> bytes:
    return default_layer().make_public_gospel_data_ret(profile)


def make_gospel_skin_data_ret(
    profile: Optional[Mapping[str, Any]], request_body: bytes
) -> bytes:
    return default_layer().make_gospel_skin_data_ret(profile, request_body)


def make_gospel_skin_status_ret(
    profile: Optional[Mapping[str, Any]], request_body: bytes
) -> bytes:
    return default_layer().make_gospel_skin_status_ret(profile, request_body)


def make_chapter_datas(
    profile: Optional[Mapping[str, Any]] = None,
    *,
    full_catalog: Optional[bool] = None,
) -> bytes:
    return default_layer().make_chapter_datas(profile, full_catalog=full_catalog)


def make_full_chapter_datas(profile: Optional[Mapping[str, Any]] = None) -> bytes:
    return default_layer().make_full_chapter_datas(profile)


def handle_content_request(
    request_id: int,
    profile: Optional[Mapping[str, Any]],
    request_body: bytes = b"",
) -> Optional[ContentResult]:
    return default_layer().handle(request_id, profile, request_body)
