"""Durable local journey: source distances, speed thresholds and rewards.

Offline policies (previous server unavailable): one free roll per observed
UTC+8 day without accumulation; reproducible uniform d6; award the destination
cell and crossed chest cells. A paid pending walk is completed on reopening.
"""
import copy
import hashlib
import json
import random
from datetime import datetime, timezone

try:
    from . import shop_content as shop
    from .inventory_content import normalize_inventory
except ImportError:
    import shop_content as shop
    from inventory_content import normalize_inventory

KEY = 'journey_state'
RESPONSES = {370: (371, 'SC_DiceDatasRet'), 372: (373, 'SC_DiceRet'),
             374: (375, 'SC_WalkReachDestinationRet')}


class JourneyLayer:
    def __init__(self, host, catalog):
        self.host = host
        self.codec = host.codec
        self.cells = sorted(catalog.table('a_journey').records, key=lambda r: r['journey'])
        self.levels = sorted(catalog.table('a_journey_level').records, key=lambda r: r['level'])
        self.diaries = catalog.table('a_journey_diary').records
        assert [r['journey'] for r in self.cells] == list(range(10, 2110, 10))

    def _state(self, profile):
        day = (int(self.host._now()) + 28800) // 86400
        state = copy.deepcopy(profile.get(KEY))
        if state is None:
            state = dict(version=1, day=day, login_days=[], free=1, total=1000,
                         remainder=0, sequence=0, pending=None, receipt=None, logs=[])
        for key in ('day', 'free', 'total', 'remainder', 'sequence'):
            shop._state_uint(state.get(key), 'journey '+key)
        if state['total'] < 1000 or state['total'] % 1000 or state['remainder'] >= 1000 or state['free'] > 1:
            raise shop.ShopApplicationError(shop.ERROR_CONFIG, 'invalid journey state')
        # Earlier observed dates can be reused, but elapsed/unplayed days do not count.
        days = set(state['login_days']) | set(profile.get('private_equipment_task_state', {}).get('login_days', []))
        if day >= state['day']:
            days.add(day)
        state['login_days'] = sorted(days)
        if day > state['day']:
            state.update(day=day, free=1)
        return state

    def _speed(self, state):
        return max((r for r in self.levels if r['cumulative_login'] <= len(state['login_days'])), key=lambda r: r['level'])

    @staticmethod
    def _position(total):
        return ((total // 1000 - 1) % 210 + 1) * 1000

    def _snapshot(self, state):
        speed = self._speed(state)
        next_level = next((r for r in self.levels if r['level'] == speed['level'] + 1), None)
        return dict(WalkSpeedLevel=speed['level'], HighestWalkSpeedLevel=speed['level'],
                    CurWalkPosition=self._position(state['total']), SumWalkDistance=state['total'],
                    RemainDiceNum=state['free'], MoveStrength=state['remainder'], LogList=state['logs'],
                    IsWalkSpeedLvlup=False, DaysLeftToNextLvl=max(0, next_level['cumulative_login']-len(state['login_days'])) if next_level else 0)

    def _rewards(self, start, steps):
        rewards = {}
        for step in range(1, steps+1):
            cell = self.cells[(start//1000-1+step) % 210]
            if step != steps and not cell['display_model']:
                continue
            for raw in json.loads(cell['journey_reward']):
                key = raw['type'], raw['id']
                rewards[key] = rewards.get(key, 0) + raw['num']
        return tuple(shop.Reward(kind, item, count) for (kind, item), count in rewards.items())

    def _complete(self, profile, state):
        pending = state['pending']
        rewards = tuple(shop.Reward(r['res_type'], r['item_id'], r['count']) for r in pending['rewards'])
        changes, actual, _ = self.host._reward_changes(profile, rewards, reject_saturated=True)
        values = self.host._reward_values(actual)
        # The calendar formatter indexes one entry per placeholder group.
        diary = next(r for r in self.diaries if r['group_type'] == len(values))
        log = dict(Id=pending['sequence'], HappenTime=pending['time'], WordId=diary['id'], RewardList=values)
        response = dict(Errorcode=0, FromRoadCell=self._position(state['total'])//1000,
                        ToRoadCell=pending['destination']//1000, CurWalkPosition=pending['destination'],
                        SumWalkDistance=state['total']+pending['distance'], Log=log,
                        MoveStrength=state['remainder'], RewardList=values)
        state.update(total=response['SumWalkDistance'], pending=None, receipt=response,
                     logs=[*state['logs'], log])
        changes['reward_history'] = [*copy.deepcopy(profile.get('reward_history', [])),
            dict(source='journey', claim_key='journey:'+str(pending['sequence']),
                 rewards=[r.as_dict() for r in actual], time=pending['time'])]
        return changes, response, actual

    def handle(self, command, profile, body=b''):
        response_id, name = RESPONSES[command]
        try:
            fields = list(shop.iter_wire_fields(body))
            if command in (370, 372) and fields:
                raise shop.ShopApplicationError(shop.ERROR_MESSAGE_DATA_ILLEGAL, 'journey request must be empty')
            if command == 374 and (len(fields) > 1 or any(n != 1 or wire != 0 or value > 0xffffffff for n, wire, value in fields)):
                raise shop.ShopApplicationError(shop.ERROR_MESSAGE_DATA_ILLEGAL, 'invalid journey destination')
            state = self._state(profile)
            changes, rewards = {}, ()
            if command == 370:
                if state['pending']:
                    changes, _, rewards = self._complete(profile, state)
                values = self._snapshot(state)
            elif command == 372:
                if state['pending']:
                    values = state['pending']['roll_response']
                else:
                    costs = []
                    if state['free']:
                        state['free'] -= 1
                    else:
                        inventory = normalize_inventory(copy.deepcopy(profile.get('inventory', {})))
                        dice = inventory.get('1001011')
                        if dice is None or dice['count'] < 1:
                            raise shop.ShopApplicationError(shop.ERROR_CLIENT_REQUEST_PARAM, 'no free or owned dice')
                        dice['count'] -= 1
                        changes['inventory'] = inventory
                        costs = [shop.Reward(4, 1001011, 1).as_dict()]
                    state['sequence'] += 1
                    seed = hashlib.sha256(f"journey-v1:{profile['role_id']}:{state['sequence']}".encode()).digest()
                    point = random.Random(int.from_bytes(seed, 'big')).randint(1, 6)
                    strength = state['remainder'] + self._speed(state)['speed'] * point
                    distance = strength//1000*1000
                    state['remainder'] = strength % 1000
                    values = dict(Errorcode=0, DicePoint=point, ToWalkDistance=distance,
                                  RemainDiceNum=state['free'], MoveStrength=strength)
                    planned = self._rewards(state['total'], distance//1000)
                    # Validate the complete grant before reserving the cost.
                    self.host._reward_changes({**profile, **changes}, planned, reject_saturated=True)
                    state['pending'] = dict(sequence=state['sequence'], destination=self._position(state['total']+distance),
                                            distance=distance, time=int(self.host._now()),
                                            rewards=[r.as_dict() for r in planned], roll_response=values)
                    changes['resource_history'] = [*copy.deepcopy(profile.get('resource_history', [])),
                        dict(source='journey_roll', sequence=state['sequence'], costs=costs, rewards=[])]
            else:
                destination = fields[0][2] if fields else 0
                if state['pending']:
                    # NewSignInView.RunAndMark clears wantRoadId before its delayed
                    # SettlementEnd callback, so the source runtime sends zero.
                    if destination not in (0, state['pending']['destination']):
                        raise shop.ShopApplicationError(shop.ERROR_CLIENT_REQUEST_PARAM, 'destination does not match reserved walk')
                    changes, values, rewards = self._complete(profile, state)
                elif state['receipt'] and destination == state['receipt']['CurWalkPosition']:
                    values = state['receipt']
                else:
                    raise shop.ShopApplicationError(shop.ERROR_CLIENT_REQUEST_PARAM, 'no pending journey')
            changes[KEY] = state
            notices = ()
            if command == 370:
                today = datetime.fromtimestamp(state['day']*86400, timezone.utc)
                login_map = {}
                rolled_days = {(r['HappenTime']+28800)//86400 for r in state['logs']}
                for day in state['login_days']:
                    date = datetime.fromtimestamp(day*86400, timezone.utc)
                    if (date.year, date.month) == (today.year, today.month):
                        login_map[date.day] = int(day in rolled_days)
                notices = ((23, self.codec.encode('SC_LoginDayDatas', {'LoginDayMap': login_map})),
                           (24, self.codec.encode('SC_SumLoginDayDatas', {
                               'Days': len(state['login_days']), 'IsTodayFirstLogin': False})))
            elif command == 372:
                day = datetime.fromtimestamp(state['pending']['time']+28800, timezone.utc).day
                notices = ((700, self.codec.encode('Notify_UpdateDayIsDiced', {'Day': day, 'IsDiced': 1})),)
            return shop.Result(response_id, self.codec.encode(name, values), changes=changes, rewards=rewards, notifications=notices)
        except (ValueError, TypeError, KeyError, StopIteration, shop.ShopApplicationError) as exc:
            code = getattr(exc, 'error_code', shop.ERROR_MESSAGE_DATA_ILLEGAL)
            # 371 has no error field; a corrupt save must fail explicitly, never
            # return an empty snapshot whose unsigned mileage underflows.
            if command == 370:
                raise ValueError('invalid journey snapshot: '+str(exc)) from exc
            return shop.Result(response_id, self.codec.encode(name, {'Errorcode': code}), error_code=code, reason=str(exc))
