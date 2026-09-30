"""Farm management offline rule v1 (not a claim of defined server maths).

Source-backed: farmer training costs/unlocks, fertilizer capacity, buff IDs and
300-second rewards. Offline design: static climate, gear growth bonuses,
600-second round-robin incidents, 60x fertilizer production, deterministic
ordinary crops. Progress uses integer fractions and time-boundary integration.
"""
from __future__ import annotations

import copy
import json

try:
    from .offline_economy import EconomyError, deduct_resources
except ImportError:
    from offline_economy import EconomyError, deduct_resources

WRITES = {626: "Watering", 628: "ApplyFertilizer", 632: "Deinsectization",
          634: "GetFertilizer", 636: "SetFacility", 638: "RepairFacility",
          640: "SetTrusteeship", 642: "FarmerTrain", 644: "RelieveStrike"}
FIELDS = {626: {1: ("LandId", 16, False), 3: ("IsGuide", 1, False)},
          628: {1: ("LandId", 16, False), 2: ("ItemId", 2**32-1, False), 3: ("IsGuide", 1, False)},
          632: {3: ("IsGuide", 1, False)}, 634: {3: ("IsGuide", 1, False)},
          636: {1: ("FacilityType", 3, False), 2: ("FacilityLvl", 3, False), 3: ("IsGuide", 1, False)},
          638: {1: ("FacilityType", 3, False), 2: ("IsGuide", 1, False)},
          640: {1: ("Opt", 1, False), 2: ("PresetTemperature", 3, False),
                3: ("PresetLightLvl", 3, False), 4: ("PresetHumidity", 3, False),
                5: ("PresetAutoHarvest", 1, False)}, 642: {}, 644: {}}


def number(value, label, low=0, high=2**63-1):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("invalid farm management " + label)
    return value


class FarmManagement:
    def __init__(self, host, tables):
        self.host = host
        self.levels = {int(r['farmer_level']): r for r in tables['a_farmer_level']}
        self.events = {int(r['id']): r for r in tables['a_farmer_event'] if r['event_type']}
        self.buffs = {int(r['id']): r for r in tables['a_farmer_buff']}
        self.facilities = {int(r['id']): r for r in tables['a_farmer_facility']}
        for level, row in self.levels.items():
            number(row['fertilizer_time'], 'fertilizer time', 1, 1000)
            if level < max(self.levels):
                costs = json.loads(row['levelup_resource'])
                if not costs or any(set(c) != {'type','id','num'} or c['type'] != 2
                                    or c['id'] != 0 or type(c['num']) is not int or c['num'] <= 0 for c in costs):
                    raise ValueError('unverified farmer training cost')

    @staticmethod
    def empty(now):
        return {'version': 1, 'last_tick': now, 'fertilizer_elapsed': 0,
                'light_lvl': 1, 'wind_lvl': 1, 'preset': {'temp': 1, 'light': 1, 'humidity': 2, 'auto': 0},
                'trusteeship_start': 0, 'event': None, 'next_event_at': now+600,
                'event_counter': 0, 'buffs': {}, 'crops': {}}

    def validate(self, farm):
        m = farm.get('management')
        if m is None:
            return None
        if not isinstance(m, dict) or set(m) != set(self.empty(0)) or type(m.get('version')) is not int or m['version'] != 1:
            raise ValueError('invalid farm management state')
        for k in ('last_tick','fertilizer_elapsed','trusteeship_start','next_event_at','event_counter'):
            number(m[k], k)
        for k in ('light_lvl','wind_lvl'):
            number(m[k], k, 1, 3)
        p = m['preset']
        if not isinstance(p, dict) or set(p) != {'temp','light','humidity','auto'}:
            raise ValueError('invalid farm preset')
        for k in ('temp','light','humidity'):
            number(p[k], k, 1, 3)
        number(p['auto'], 'auto harvest', 0, 1)
        number(farm['trusteeship'], 'trusteeship', 0, 1)
        e = m['event']
        if e is not None:
            if not isinstance(e, dict) or set(e) != {'id','started'} or e['id'] not in self.events:
                raise ValueError('invalid farm event')
            number(e['started'], 'event start', 0, m['last_tick'])
        if not isinstance(m['buffs'], dict) or not isinstance(m['crops'], dict):
            raise ValueError('invalid farm buffs/crops')
        for key, row in m['buffs'].items():
            if key not in ('1806006','1806007','1806008') or not isinstance(row, dict) or set(row) != {'start','end'}:
                raise ValueError('invalid farm positive buff')
            number(row['start'], 'buff start', 0, m['last_tick'])
            number(row['end'], 'buff end', row['start']+300, row['start']+300)
        for key, row in m['crops'].items():
            if key not in {str(i) for i in range(1,17)} or not isinstance(row, dict) or set(row) != {'generation','fraction','feed_count'}:
                raise ValueError('invalid managed crop')
            number(row['generation'], 'generation', 1, 2**32-1)
            number(row['fraction'], 'growth fraction', 0, 9999)
            number(row['feed_count'], 'fertilizer applications', 0, 5)
        return m

    def activate(self, farm, now):
        if 'management' not in farm:
            farm['management'] = self.empty(now)
        return self.validate(farm)

    @staticmethod
    def tracked(farm, land):
        row = farm.get('management', {}).get('crops', {}).get(str(land['land_id']))
        return row if row and row['generation'] == land['generation'] and land['seed_id'] else None

    @staticmethod
    def adopt(farm, land):
        m = farm.get('management')
        if m is not None:
            row = m['crops'].get(str(land['land_id']))
            if not row or row['generation'] != land['generation']:
                m['crops'][str(land['land_id'])] = {'generation': land['generation'], 'fraction': 0, 'feed_count': 0}

    @staticmethod
    def home_level(profile):
        value = profile.get('buildings', {}).get('1999705', 0)
        return int(value.get('level', 0) if isinstance(value, dict) else value)

    def facility_status(self, profile, farm, kind):
        # Source a_farmer_facility unlock_work_level -> a_work_level: home 4/3/1.
        if self.home_level(profile) < {1: 4, 2: 3, 3: 1}[kind]:
            return 0
        e = farm.get('management', {}).get('event')
        return 2 if e and self.events[e['id']]['event_type'] == kind else 3

    def event_type(self, farm):
        event = farm.get('management', {}).get('event')
        return self.events[event['id']]['event_type'] if event else 0

    def rate(self, profile, farm, land):
        """Harvest index / second, represented as a numerator over 10000."""
        m = farm['management']
        modifier = 10000
        if self.facility_status(profile, farm, 1) == 3:
            modifier += 500 * (m['light_lvl']-1)
        if self.facility_status(profile, farm, 2) == 3:
            modifier += 500 * (m['wind_lvl']-1)
        if '1806006' not in m['buffs']:
            if land['land_humidity'] < 101 or self.facility_status(profile, farm, 3) == 2:
                modifier -= 2000
        if self.event_type(farm) == 4:
            modifier -= 2000
        if '1806007' in m['buffs']:
            modifier += 2000
        return self.levels[farm['farmer_level']]['grow_speed'] * modifier // 1000

    def advance(self, profile, farm, now):
        m = self.validate(farm)
        if m is None:
            return
        now = max(now, m['last_tick'])
        start = m['last_tick']
        level = self.levels[farm['farmer_level']]
        period, cap = level['fertilizer_time'] * 60, level['fertilizer_limit']
        if farm['available_fertilizer_num'] < cap:
            total = m['fertilizer_elapsed'] + now-start
            gained, rem = divmod(total, period)
            farm['available_fertilizer_num'] = min(cap, farm['available_fertilizer_num']+gained)
            m['fertilizer_elapsed'] = rem if farm['available_fertilizer_num'] < cap else 0
        else:
            m['fertilizer_elapsed'] = 0
        # At most one incident may start, and three timed buffs may expire.
        # No loop proportional to offline duration, no rerolls on reload.
        cursor = start
        while True:
            m['buffs'] = {k: v for k,v in m['buffs'].items() if v['end'] > cursor}
            growing = any(l['seed_id'] and l['harvest_value'] < self.host.catalog.seeds[l['seed_id']].harvest_index
                          and self.tracked(farm, l) for l in farm['lands'])
            if not m['event'] and m['next_event_at'] <= cursor:
                if growing:
                    eligible = [e for e in self.events.values() if e['unlock_level'] <= farm['farmer_level']
                                and (e['event_type'] not in (1,2,3) or self.facility_status(profile, farm, e['event_type']) == 3)
                                and not (e['event_type'] == 5 and '1806008' in m['buffs'])]
                    event = eligible[m['event_counter'] % len(eligible)]
                    m['event'] = {'id': event['id'], 'started': cursor}
                    m['event_counter'] += 1
                else:
                    m['next_event_at'] = cursor + ((now-cursor)//600+1)*600
            if cursor >= now:
                break
            boundaries = [now] + [b['end'] for b in m['buffs'].values() if b['end'] > cursor]
            if not m['event'] and m['next_event_at'] > cursor:
                boundaries.append(m['next_event_at'])
            end = min(boundaries)
            for land in farm['lands']:
                row = self.tracked(farm, land)
                if row is None:
                    continue
                seed = self.host.catalog.seeds[land['seed_id']]
                total = row['fraction'] + (end-cursor) * self.rate(profile, farm, land)
                add, row['fraction'] = divmod(total, 10000)
                land['harvest_value'] = min(seed.harvest_index, land['harvest_value']+add)
                if land['harvest_value'] == seed.harvest_index:
                    row['fraction'] = 0
            cursor = end
        m['last_tick'] = now
        farm['last_clock'] = now
        m['crops'] = {str(l['land_id']): m['crops'][str(l['land_id'])] for l in farm['lands'] if self.tracked(farm,l)}

    def farm_wire(self, profile, farm):
        m = farm.get('management', self.empty(0))
        p = m['preset']
        return {'FarmerLvl': farm['farmer_level'], 'CurrentTemperature': farm['current_temperature'],
                'LightFacility': self.facility_status(profile, farm, 1), 'LightLvl': m['light_lvl'],
                'BlowFacility': self.facility_status(profile, farm, 2), 'BlowLvl': m['wind_lvl'],
                'IrrigationFacility': self.facility_status(profile, farm, 3),
                'AvailableFertilizerNum': farm['available_fertilizer_num'], 'Trusteeship': farm['trusteeship'],
                'PresetTemperature': p['temp'], 'PresetLightLvl': p['light'], 'PresetHumidity': p['humidity'],
                'PresetAutoHarvest': p['auto'], 'Strike': int(self.event_type(farm) == 5),
                'TrusteeshipStartTime': m['trusteeship_start'], 'LastFertilizerRefreshTime':
                    m['last_tick']-m['fertilizer_elapsed'] if farm['available_fertilizer_num'] < self.levels[farm['farmer_level']]['fertilizer_limit'] else 0,
                'LastEventRefreshTime': m['event']['started'] if m['event'] else 0,
                'Signature': 'Offline farm v1: standard crops; training and tiers affect growth; fertilizer output is 60x; at most one event per 10 minutes.'}

    def buff_wire(self, farm):
        m = farm.get('management', self.empty(0))
        rows = {int(k): v for k,v in m['buffs'].items()}
        if m['event']:
            rows[self.events[m['event']['id']]['event_debuff']] = {'start': m['event']['started'], 'end': 0}
        return [{'BuffUId': bid, 'BuffId': bid, 'BuffType': self.buffs[bid]['buff_type'],
                 'StartTimeStamp': row['start'], 'EndTimeStamp': row['end'], 'Param1': 5 if bid == 1806005 else 0}
                for bid,row in sorted(rows.items())]

    def notifications(self, profile, farm, now, force=True):
        buffs = self.buff_wire(farm)
        old = profile.get('oracle_content',{}).get('farm')
        notes = []
        wire = self.farm_wire(profile,farm)
        if force or old is None or self.farm_wire(profile,old) != wire:
            notes.append((655,self.host.codec.encode('Notify_UpdateFarmDatas',{'Farm':wire})))
        notes.append(self.host._notify(self.host._available_lands(farm),now))
        if force or old is None or self.buff_wire(old) != buffs:
            notes.append((657,self.host.codec.encode('Notify_UpdateFarmBuffDatas',{'FarmBuffList':buffs,
                  'DelFarmBuffList':[{'BuffUId':bid,'BuffId':bid} for bid in self.buffs if bid not in {b['BuffId'] for b in buffs}]})))
        event = farm.get('management',{}).get('event')
        if event and (old or {}).get('management',{}).get('event') != event:
            notes.append((646,self.host.codec.encode('Notify_FarmEvent',{'EventType':self.events[event['id']]['event_type']})))
        return tuple(notes)

    def resolve_event(self, farm, now):
        m = farm['management']
        event = self.events[m['event']['id']]
        bid = event['event_buff']
        m['buffs'][str(bid)] = {'start': now, 'end': now+self.buffs[bid]['event_buff_time']}
        m['event'] = None
        m['next_event_at'] = now+600

    def automatic_harvest(self, profile, root, now):
        """Query catch-up, one ordinary crop per generation. Full stacks retry.

        Queries don't plant or spend. An incident of type strike pauses only
        automation; manual watering/harvest and resolving the incident remain.
        """
        farm = root['farm']
        m = farm.get('management')
        if (not m or not farm['trusteeship'] or not m['preset']['auto'] or farm['farmer_level'] < 10
                or self.event_type(farm) == 5 or not self.host._farm_write_access(profile)[0]):
            return {}
        candidate = copy.deepcopy(profile)
        changes = {}
        for land in farm['lands']:
            seed = self.host.catalog.seeds.get(land['seed_id'])
            if not seed or land['harvest_value'] < seed.harvest_index:
                continue
            try:
                inventory = self.host._seed_inventory(candidate, seed.crop_ids[0], 1)
            except ValueError as exc:
                if 'cap' in str(exc):
                    continue
                raise
            key = f"farm:{land['land_id']}:{land['generation']}"
            reward_history = self.host._history(candidate, 'reward_history')
            if any(r.get('claim_key') == key for r in reward_history):
                raise ValueError('farm generation already rewarded while still planted')
            rewards = [{'type':4, 'id':seed.crop_ids[0], 'num':1}]
            row = {'source':'farm', 'command_id':693, 'operation':'auto_harvest',
                   'land_ids':[land['land_id']], 'plant_generation':[land['generation']], 'rewards':rewards}
            reward_history.append(dict(row, claim_key=key))
            resource_history = self.host._history(candidate, 'resource_history')
            resource_history.append(dict(row, costs=[]))
            candidate.update(inventory=inventory, reward_history=reward_history, resource_history=resource_history)
            changes.update({k:candidate[k] for k in ('inventory','reward_history','resource_history')})
            generation = land['generation']
            land.update(self.host._empty_land(land['land_id']))
            land['generation'] = generation
            m['crops'].pop(str(land['land_id']), None)
        return changes

    def parse_intent(self, command, body, target):
        try:
            from .farm_content import _decode_request, _scalar
        except ImportError:
            from farm_content import _decode_request, _scalar
        parsed = _decode_request(body, 'CS_'+WRITES[command], FIELDS[command])
        expected = _scalar(parsed, 1) if command in (626,628,636,638) else 1
        if expected != target or target < 1:
            raise ValueError('farm intent target mismatch')

    def handle(self, command, profile, body):
        try:
            from .farm_content import FarmResult, _decode_request, _scalar
        except ImportError:
            from farm_content import FarmResult, _decode_request, _scalar
        message = 'SC_'+WRITES[command]+'Ret' if command in WRITES else 'SC_FarmBuffEndTimeRet'
        try:
            fields = FIELDS[command] if command in WRITES else {1:('BuffId',2**32-1,False)}
            parsed = _decode_request(body, message.replace('SC_', 'CS_').removesuffix('Ret'), fields,
                                     allow_operation_id=command in WRITES)
            allowed, reason = self.host._farm_write_access(profile)
            if command in WRITES and not allowed:
                raise EconomyError(reason, 32772)
            root, now = self.host._state(profile, self.host._now())
            farm = root['farm']
            m = self.activate(farm, now)
            values, costs, rewards, changes = {'Errorcode':0}, [], [], {}
            if command in (626,628):
                land = self.host._land(farm, _scalar(parsed,1))
                if not land['seed_id']:
                    raise EconomyError('land is empty',122)
                seed = self.host.catalog.seeds[land['seed_id']]
                self.adopt(farm, land)
                row = self.tracked(farm, land)
                values['LandId'] = land['land_id']
                if command == 626:
                    land['land_humidity'] = 300
                    values['Humidity'] = 300
                else:
                    if _scalar(parsed,2) != self.host.catalog.fertilizer_item_id:
                        raise ValueError('unsupported fertilizer')
                    if land['harvest_value'] >= seed.harvest_index or row['feed_count'] >= 5:
                        raise EconomyError('crop mature or fertilizer limit reached',122)
                    costs = [{'type':4, 'id':self.host.catalog.fertilizer_item_id, 'num':1}]
                    changes.update(deduct_resources(profile, [(c['type'],c['id'],c['num']) for c in costs]))
                    row['feed_count'] += 1
                    land['harvest_value'] = min(seed.harvest_index, land['harvest_value']+seed.harvest_index//10)
                    values['ItemId'] = self.host.catalog.fertilizer_item_id
            elif command == 634:
                count = farm['available_fertilizer_num']
                if not count:
                    raise EconomyError('no fertilizer ready',122)
                changes['inventory'] = self.host._seed_inventory(profile,self.host.catalog.fertilizer_item_id,count)
                rewards = [{'type':4, 'id':self.host.catalog.fertilizer_item_id, 'num':count}]
                farm['available_fertilizer_num'] = 0
                m['fertilizer_elapsed'] = 0
                values['ItemNum'] = count
            elif command == 642:
                current = farm['farmer_level']
                if current+1 not in self.levels:
                    raise EconomyError('farmer already at maximum level',122)
                costs = json.loads(self.levels[current]['levelup_resource'])
                changes.update(deduct_resources(profile,[(c['type'],c['id'],c['num']) for c in costs]))
                farm['farmer_level'] = current+1
                farm['farmer_level_config_id'] = self.levels[current+1]['id']
                values['FarmerLvl'] = current+1
            elif command in (636,638):
                kind = _scalar(parsed,1)
                if kind not in (1,2,3) or self.facility_status(profile,farm,kind) == 0:
                    raise EconomyError('facility not unlocked',32772)
                values['FacilityType'] = kind
                if command == 638:
                    if self.facility_status(profile,farm,kind) != 2:
                        raise EconomyError('facility does not need repair',122)
                    self.resolve_event(farm, now)
                else:
                    grade = _scalar(parsed,2)
                    if kind not in (1,2) or grade not in (1,2,3) or self.facility_status(profile,farm,kind) != 3:
                        raise ValueError('invalid/broken adjustable facility')
                    m['light_lvl' if kind == 1 else 'wind_lvl'] = grade
                    farm['current_temperature'] = max(1,min(300,100+50*(m['light_lvl']-m['wind_lvl'])))
                    values['FacilityLvl'] = grade
            elif command == 640:
                opt = _scalar(parsed,1)
                if opt and farm['farmer_level'] < self.facilities[1804006]['unlock_level']:
                    raise EconomyError('trusteeship requires farmer level 6',32772)
                if opt:
                    p = {'temp':_scalar(parsed,2), 'light':_scalar(parsed,3), 'humidity':_scalar(parsed,4), 'auto':_scalar(parsed,5)}
                    if any(p[k] not in (1,2,3) for k in ('temp','light','humidity')):
                        raise ValueError('preset climate must be 1..3')
                    if p['auto'] and farm['farmer_level'] < self.facilities[1804007]['unlock_level']:
                        raise EconomyError('automatic harvest requires farmer level 10',32772)
                    m['preset'] = p
                    m['light_lvl'] = p['light']
                    farm['current_temperature'] = p['temp']*100-50
                    for land in farm['lands']:
                        if land['seed_id'] and self.tracked(farm,land):
                            land['land_humidity'] = p['humidity']*100-50
                    if not farm['trusteeship']:
                        m['trusteeship_start'] = now
                farm['trusteeship'] = opt
                values['Opt'] = opt
            elif command in (632,644):
                if self.event_type(farm) != (4 if command == 632 else 5):
                    raise EconomyError('no matching farm incident',122)
                self.resolve_event(farm, now)
            elif command == 660:
                bid = _scalar(parsed,1)
                if bid not in self.buffs:
                    raise ValueError('unknown farm buff')
                values.update(BuffId=bid,Time=max(0,m['buffs'].get(str(bid),{}).get('end',now)-now))
            if command in WRITES:
                history = self.host._history(profile,'resource_history')
                history.append({'source':'farm', 'command_id':command, 'operation':WRITES[command], 'costs':costs, 'rewards':rewards})
                changes['resource_history'] = history
            changes.update(self.host._changes(profile,root))
            return FarmResult(command+1,self.host.codec.encode(message,values),changes=changes,
                              notifications=self.notifications(profile,farm,now))
        except (ValueError,KeyError,TypeError) as exc:
            code = getattr(exc,'error_code',32769)
            return FarmResult(command+1,self.host.codec.encode(message,{'Errorcode':code}),error_code=code,supported=False,reason=str(exc))
