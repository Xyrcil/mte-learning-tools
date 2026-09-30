"""Eight-stage tasks, progress tracking, and atomic claims.

Visibility/all-unlocked seeds never imply legacy actions. Durable account
facts and successful action ledgers supply progress; peaks survive resetting
or unequipping an entity. Source reward tables are the sole grant source.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

try:
    from .content_responses import iter_wire_fields, ProtobufDecodeError
    from .shop_content import Reward, ShopApplicationError
    from .battle_settlement import Reward as BattleReward
except ImportError:
    from content_responses import iter_wire_fields, ProtobufDecodeError
    from shop_content import Reward, ShopApplicationError
    from battle_settlement import Reward as BattleReward

RESPONSE_IDS = {592: 593, 594: 595, 596: 597}
KINDS = {6043, 3000, 2000, 6009, 2085, 7119, 7114, 7115, 7103,
         7108, 6005, 3011, 7118, 2069, 6052, 7116, 7112, 6001}


@dataclass(frozen=True)
class StageResult:
    response_id: int
    body: bytes
    changes: dict = field(default_factory=dict)
    error_code: int = 0
    reason: str = ""
    notifications: tuple = ()
    rewards: tuple = ()

    @property
    def ok(self):
        return self.error_code == 0


class StageTaskLayer:
    def __init__(self, catalog, shop, battle):
        self.catalog, self.shop, self.battle, self.codec = catalog, shop, battle, shop.codec
        self.achievement_snapshot = None
        self.tasks = {int(r['id']): r for r in catalog.table('a_stage_task').records}
        self.stages = {int(r['count_stage']): r for r in catalog.table('a_stage_task_reward').records}
        self.conditions = {int(r['achievement']): catalog.record('a_condition', int(r['achievement']))
                           for r in self.tasks.values()}
        if set(self.stages) != set(range(1510001, 1510009)) or len(self.tasks) != 40:
            raise ValueError('unexpected stage catalog')
        if {int(c['achievements_type']) for c in self.conditions.values()} - KINDS:
            raise ValueError('stage condition lacks an evaluator')
        self.groups = {stage: {i for i, r in self.tasks.items() if r['count_stage_id'] == stage}
                       for stage in self.stages}
        self.chapter_dungeons = {}
        for r in catalog.table('a_dungeon').records:
            key = (int(r['belong_chapte']), int(r['difficulty_type']))
            self.chapter_dungeons.setdefault(key, set()).add(int(r['id']))

    def state(self, profile):
        state = copy.deepcopy(profile.get('stage_task_state', {}))
        if not isinstance(state, dict):
            raise ValueError('invalid stage task state')
        for key, known in (('claimed_tasks', self.tasks), ('claimed_stages', self.stages)):
            values = state.setdefault(key, [])
            if (not isinstance(values, list) or any(type(v) is not int or v not in known for v in values)
                    or len(set(values)) != len(values)):
                raise ValueError('invalid stage claim history')
        if sorted(state['claimed_stages']) != list(range(1510001, 1510001 + len(state['claimed_stages']))):
            raise ValueError('noncontiguous claimed stages')
        if any(not self.groups[s] <= set(state['claimed_tasks']) for s in state['claimed_stages']):
            raise ValueError('stage reward lacks claimed prerequisites')
        peaks = state.setdefault('progress', {})
        if (not isinstance(peaks, dict) or any(str(i) not in {str(c) for c in self.conditions}
                or type(n) is not int or not 0 <= n <= 0xffffffff for i, n in peaks.items())):
            raise ValueError('invalid stage progress')
        return state

    @staticmethod
    def _spent(history, kind):
        return sum(max(0, int(c.get('num', c.get('count', 0))))
                   for row in history for c in row.get('costs', [])
                   if c.get('type', c.get('res_type')) == kind)

    def metric(self, profile, condition):
        kind = int(condition['achievements_type'])
        p1, p2 = (int(condition[f'achievements_parameter{n}']) for n in (1, 2))
        history = profile.get('resource_history', [])
        life = profile.get('life_content', {})
        transactions = life.get('transactions', [])
        completed = set(profile.get('completed_dungeons', []))
        if kind == 6043: return sum(r.get('kind') == 'cook_food' for r in transactions)
        if kind == 3000: return int(p1 in completed)
        if kind == 2000:
            return max(self._spent(history, 3), int(profile.get('private_equipment_task_state', {}).get('consumed', {}).get('energy', 0)))
        if kind == 6052: return self._spent(history, 2)
        if kind == 6009:
            required = self.chapter_dungeons.get((p1, p2), set())
            return int(bool(required) and required <= completed)
        if kind == 2085:
            return int(any(a.get('decorations') for h in profile.get('hero_appearances', {}).values()
                           for a in h.get('appearances', [])))
        if kind == 7119: return len(set(profile.get('private_equipment_task_state', {}).get('login_days', [])))
        if kind == 7114: return sum(r.get('kind') == 'hero_eat' for r in transactions)
        if kind == 7115: return len(profile.get('draw_history', []))
        if kind == 7103:
            return sum(max(0, int(r.get('context', {}).get('to_level', 0)) - int(r.get('context', {}).get('from_level', 0)))
                       for r in history if r.get('source') == 'hero_progression' and r.get('operation') == 'unlock_talent')
        if kind == 7108:
            return sum(r.get('source') == 'gospel_progression' and r.get('operation') == 'skill_enhance' for r in history)
        if kind == 6005: return int(profile.get('sophisticate_level', 0))
        if kind == 3011: return int(profile.get('level', 1))
        if kind == 7118: return len({r['id'] for r in profile.get('gospels', []) if int(r.get('level', 1)) >= p1})
        if kind == 2069:
            # Current completion and durable claims are two views of the same
            # daily tasks, not two independent actions to add together.
            ids = {int(r['task_id']) for r in profile.get('task_reward_history', [])
                   if r.get('cycle_type') == 0 and r.get('claimed')}
            ids.update(int(i) for i, r in profile.get('tasks', {}).get('daily', {}).items()
                       if isinstance(r, dict) and int(r.get('status', 0)) >= 1)
            return len(ids)
        if kind == 7116:
            return sum(len(b.get('claim_history', [])) for b in profile.get('oracle_content', {}).get('work', {}).values())
        if kind == 7112: return len({r['medal_id'] for r in profile.get('medals', [])})
        if kind == 6001: return len({r['id'] for r in profile.get('gospels', [])})
        raise ValueError('unhandled stage condition')

    def achievements(self, profile):
        peaks = self.state(profile)['progress']
        rows = []
        for aid, c in sorted(self.conditions.items()):
            target = int(c['achievements_parameter3'])
            actual = min(target, max(0, self.metric(profile, c), int(peaks.get(str(aid), 0))))
            rows.append({'Id': aid, 'Type': int(c['achievements_type']), 'Progress': actual,
                         'Status': int(actual >= target)})
        return rows

    def observe(self, profile):
        state = self.state(profile)
        for row in self.achievements(profile):
            if row['Progress']: state['progress'][str(row['Id'])] = row['Progress']
        if state != self.state(profile): profile['stage_task_state'] = state

    def snapshot(self, profile):
        state = self.state(profile)
        ready = {r['Id']: r['Status'] for r in self.achievements(profile)}
        return {'StageIdList': sorted(state['claimed_stages']), 'TaskList': [
            {'Id': i, 'Status': 2 if i in state['claimed_tasks'] else ready[int(r['achievement'])]}
            for i, r in sorted(self.tasks.items())]}

    def notifications(self, profile):
        return ((320, self.codec.encode('Notify_UpdateAchieve', {'AchieveList': self.achievements(profile)})),
                (829, self.codec.encode('Notify_UpdateStageTask', {'TaskList': self.snapshot(profile)['TaskList']})))

    def handle(self, command, profile, body):
        if command not in RESPONSE_IDS: return None
        response = RESPONSE_IDS[command]
        message = {592: 'SC_RequestStageTaskDataRet', 594: 'SC_TakeStageTaskRewardRet', 596: 'SC_TakeStageRewardRet'}[command]
        values = {} if command == 592 else {'Errorcode': 0, 'StageId': 0, 'RewardList': []}
        def error(code, reason):
            return StageResult(response, self.codec.encode(message, {**values, **({'Errorcode': code} if command != 592 else {})}),
                               error_code=code, reason=reason)
        try:
            if command == 592:
                if body: return error(32769, 'expected empty stage query')
                # Seed all achieve IDs before StageTaskItem dereferences them.
                return StageResult(response, self.codec.encode(message, self.snapshot(profile)),
                    notifications=((317, self.achievement_snapshot(profile).body if self.achievement_snapshot else
                        self.codec.encode('SC_AchieveDatasRet', {'AchieveList': self.achievements(profile)})),))
            decoded = {}
            for number, wire, raw in iter_wire_fields(bytes(body)):
                if number not in ({1, 2, 3} if command == 594 else {1}) or number in decoded or wire != 0 or raw > 0xffffffff:
                    return error(32769, 'invalid stage claim fields')
                decoded[number] = int(raw)
            state = self.state(profile)
            claimed = set(state['claimed_tasks']); stages = set(state['claimed_stages'])
            status = {r['Id']: r['Status'] for r in self.snapshot(profile)['TaskList']}
            task = decoded.get(1, 0) if command == 594 else 0
            take_all = decoded.get(3, 0)
            stage = decoded.get(2, 0) if command == 594 else decoded.get(1, 0)
            if command == 594:
                values.update(StageTaskId=task, TakeAll=bool(take_all))
                if take_all not in (0, 1) or (take_all and task) or (not take_all and task not in self.tasks):
                    return error(32769, 'invalid task selection')
                if not take_all:
                    actual_stage = int(self.tasks[task]['count_stage_id'])
                    if stage not in (0, actual_stage): return error(32769, 'task and stage do not match')
                    stage = actual_stage
            values['StageId'] = stage
            if stage not in self.stages: return error(32769, 'unknown stage ID')
            if stage > 1510001 + len(stages): return error(5504, 'previous stage reward is not claimed')
            if command == 596:
                if stage in stages: return error(5505, 'stage reward already claimed')
                if not self.groups[stage] <= claimed: return error(5504, 'claim all stage tasks first')
                rows = [self.stages[stage]]
                state['claimed_stages'] = sorted(stages | {stage})
            else:
                selected = sorted(i for i in self.groups[stage] if status[i] == 1) if take_all else [task]
                if not selected: return error(122, 'no completed unclaimed task')
                if any(status[i] != 1 for i in selected):
                    return error(5505 if task in claimed else 5504, 'task already claimed or not completed')
                rows = [self.tasks[i] for i in selected]
                state['claimed_tasks'] = sorted(claimed | set(selected))
            rewards = self.shop._aggregate(Reward(r['type'], r['id'], r['num'])
                for row in rows for r in json.loads(row['sign_awards']))
            heroes = tuple(r for r in rewards if r.res_type == 6)
            changes, actual, _ = self.shop._reward_changes(profile, tuple(r for r in rewards if r.res_type != 6), reject_saturated=True)
            if heroes:
                # Same level-one UID/ownership rules as source dungeon rewards;
                # an already owned unique witch is retained, never duplicated.
                changes.update(self.battle.grant_rewards({**profile, **changes}, tuple(BattleReward(r.res_type, r.item_id, r.count) for r in heroes)))
                actual = (*actual, *heroes)
            for row in self.achievements(profile):
                if row['Progress']: state['progress'][str(row['Id'])] = row['Progress']
            changes['stage_task_state'] = state
            entry = {'source': 'stage_tasks', 'command_id': command, 'stage_id': stage,
                     'task_ids': selected if command == 594 else [], 'costs': [],
                     'rewards': [r.as_dict() for r in actual]}
            changes['resource_history'] = [*copy.deepcopy(profile.get('resource_history', [])), entry]
            changes['reward_history'] = [*copy.deepcopy(profile.get('reward_history', [])), entry]
            values['RewardList'] = self.shop._reward_values(actual)
            return StageResult(response, self.codec.encode(message, values), changes=changes,
                notifications=self.notifications({**profile, **changes}), rewards=tuple(actual))
        except ShopApplicationError as exc:
            return error(exc.error_code, str(exc))
        except (ValueError, TypeError, KeyError, ProtobufDecodeError) as exc:
            return error(32769, str(exc))
