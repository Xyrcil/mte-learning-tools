"""Versioned offline horse racing; source Unity course/skills decide ranks.

Stake/quota/horse IDs are source-backed. Course generation and rank payouts
are explicit local rules, not a implementation of the absent online server.
All results are copy-on-write; dispatch commits receipts with the same state.
"""
from __future__ import annotations
import base64
import copy
import hashlib
import json
import math
import re
import struct
import time
from dataclasses import dataclass, field, replace

try:
    from .content_profile import ContentCatalog
    from .content_responses import iter_wire_fields
    from .inventory_content import normalize_inventory, stable_item_uid, make_update_item
    from .offline_economy import deduct_resources, EconomyError
    from .protobuf_codec import ProtocolMessageCodec, encode_bytes
    from . import mutation_receipts
except ImportError:
    from content_profile import ContentCatalog
    from content_responses import iter_wire_fields
    from inventory_content import normalize_inventory, stable_item_uid, make_update_item
    from offline_economy import deduct_resources, EconomyError
    from protobuf_codec import ProtocolMessageCodec, encode_bytes
    import mutation_receipts

KEY = 'race_game'
RESPONSE_IDS = {482: 483, 484: 485, 486: 487}
HORSES = (2016002, 2016003, 2016004, 2016005)
COIN = 1001003
BUILDING = 1999703
NAMES = {484: 'SC_BetRaceHorseRet', 486: 'SC_SettleRaceHorseRet'}


@dataclass(frozen=True)
class RaceResult:
    response_id: int
    body: bytes
    changes: dict = field(default_factory=dict)
    notifications: tuple = ()
    error_code: int = 0
    reason: str = ''
    @property
    def ok(self): return self.error_code == 0


def number(v, lo=0, hi=2**53-1):
    if type(v) is not int or not lo <= v <= hi:
        raise ValueError('invalid race integer')
    return v


def token(v):
    if not isinstance(v, str) or re.fullmatch('[0-9a-f]{32}', v) is None:
        raise ValueError('invalid race identity')
    return v


def fields(body, allowed, repeated=()):
    out = {}
    for n, w, v in iter_wire_fields(body):
        if n not in allowed or w != allowed[n] or (n in out and n not in repeated):
            raise ValueError('invalid or duplicate race field')
        out.setdefault(n, []).append(v)
    return out


def scalar(data, n, default=0): return data.get(n, [default])[0]


def string(data, n): return token(scalar(data, n, b'').decode('ascii'))


class RaceContentLayer:
    def __init__(self, catalog, *, codec=None, clock=None):
        self.catalog = catalog if isinstance(catalog, ContentCatalog) else ContentCatalog.load(catalog)
        self.codec = codec or ProtocolMessageCodec()
        self.clock = clock or time.time
        row = self.catalog.record('a_global', 9990188)
        self.stake = number(row['int_value'], 1)
        # The source HorseRace UI consumes item 1001003 and multiplies
        # a_global[9990188].int_value by BetNum.
        if self.stake != 500 or json.loads(row['string_value']) != [{'type':4,'id':COIN,'num':500}]:
            raise ValueError('race stake source changed')
        self.maximum = number(self.catalog.record('a_item', COIN)['max_stack_num'], 1)
        self.quotas = {number(r['work_level']): number(r['gambling_count_limit'], 0, 30)
                       for r in self.catalog.table('a_work_level').records if r['building_id'] == BUILDING}
        ids = tuple(sorted(r['id'] for r in self.catalog.table('a_horse').records if r['appear_weight'] > 0))
        if ids != HORSES or self.quotas != {0:0, 1:30, 2:30, 3:30, 4:30, 5:30}:
            raise ValueError('race source closure changed')
        self.profits = [self.stake*n for n in (4,3,2,1)]

    def error(self, command, reason, code=32769, round_id=None):
        body = self.codec.encode(NAMES[command], {'Errorcode': code})
        if round_id: body += encode_bytes(126, round_id.encode())
        return RaceResult(RESPONSE_IDS[command], body, error_code=code, reason=reason)

    def parse(self, command, body, *, journal=False, target=None):
        if not isinstance(body, bytes) or len(body) > 6000: raise ValueError('race request too large')
        data = fields(body, {1:0,2:0,3:0,127:2} if command == 484 else {1:2,2:0,126:2,127:2}, (1,) if command == 486 else ())
        if journal:
            if 127 in data: raise ValueError('journal contains transport identity')
        else: string(data, 127)
        guide = number(scalar(data, 3 if command == 484 else 2), 0, 1)
        if guide: raise ValueError('guide-only race is not a paid offline round')
        if command == 484:
            horse, units = number(scalar(data, 1)), number(scalar(data, 2), 1, 10)
            if horse not in HORSES or (target is not None and target != horse): raise ValueError('unknown race horse')
            return {'horse':horse, 'units':units}
        if target is not None and target != 1: raise ValueError('invalid settlement journal target')
        round_id = string(data, 126)
        rows = data.get(1, [])
        if len(rows) != 4: raise ValueError('race needs exactly four finishers')
        result = []
        for raw in rows:
            rank = fields(raw, {1:0,2:0,3:2}, (3,))
            horse, place = number(scalar(rank,1)), number(scalar(rank,2),1,4)
            if horse not in HORSES: raise ValueError('unadmitted horse')
            speeds = rank.get(3, [])
            if len(speeds) > 64: raise ValueError('too many speed samples')
            seen = set()
            for raw_speed in speeds:
                sample = fields(raw_speed, {1:0,2:5})
                index = number(scalar(sample,1),0,1_000_000)
                raw_float = scalar(sample,2,b'\0'*4)
                speed = struct.unpack('<f',raw_float)[0]
                if index in seen or not math.isfinite(speed) or not 0 <= speed <= 10:
                    raise ValueError('invalid race speed sample')
                seen.add(index)
            result.append({'HorseId':horse, 'Rank':place})
        if {r['HorseId'] for r in result} != set(HORSES) or {r['Rank'] for r in result} != {1,2,3,4}:
            raise ValueError('duplicate race horse or rank')
        return {'round_id':round_id, 'ranks':sorted(result,key=lambda r:r['Rank'])}

    def state(self, profile):
        now = number(int(self.clock()))
        raw = profile.get(KEY)
        if raw is None:
            old = profile.get('oracle_content',{}).get('race',{}).get('remaining_bets',30)
            s = {'version':1,'day':(now+28800)//86400,'last_clock':now,'remaining':number(old,0,30),
                 'sequence':0,'rng_counter':0,'active':None,'last':None}
        else:
            s = copy.deepcopy(raw)
            if not isinstance(s,dict) or set(s) != {'version','day','last_clock','remaining','sequence','rng_counter','active','last'} or type(s['version']) is not int or s['version'] != 1:
                raise ValueError('invalid race state')
            for k in ('day','last_clock','sequence','rng_counter'): number(s[k])
            number(s['remaining'],0,30)
            for r in (s['active'],s['last']):
                if r is not None: self.validate_round(r)
            if s['active'] is not None and s['active']['status'] not in ('running','pending'):
                raise ValueError('invalid active race status')
            if s['last'] is not None and s['last']['status'] not in ('refunded','settled'):
                raise ValueError('invalid historical race status')
        now = max(now,s['last_clock'])
        day = (now+28800)//86400
        if day > s['day']: s.update(day=day,remaining=30)
        s['last_clock'] = now
        return s,now

    def validate_round(self, r):
        if not isinstance(r,dict) or set(r) != {'id','horse','units','stake','started','day','seed','ranks','status','award'}:
            raise ValueError('invalid saved race round')
        token(r['id']); token(r['seed'])
        if r['horse'] not in HORSES: raise ValueError('invalid saved horse')
        units = number(r['units'],1,10)
        if number(r['stake']) != units*self.stake: raise ValueError('invalid saved stake')
        for k in ('started','day','award'): number(r[k])
        if r['status'] not in ('running','pending','settled','refunded'): raise ValueError('invalid race status')
        if r['ranks'] is not None:
            ranks = r['ranks']
            if (not isinstance(ranks,list) or len(ranks) != 4 or any(not isinstance(v,dict) or set(v) != {'HorseId','Rank'} for v in ranks)
                    or {number(v['HorseId']) for v in ranks} != set(HORSES) or {number(v['Rank'],1,4) for v in ranks} != {1,2,3,4}):
                raise ValueError('invalid saved finishers')
        if (r['status'] in ('pending','settled')) != (r['ranks'] is not None): raise ValueError('inconsistent saved finishers')
        expected = self.profits[next(v['Rank'] for v in r['ranks'] if v['HorseId']==r['horse'])-1]*units if r['status']=='settled' else 0
        if r['award'] != expected: raise ValueError('invalid saved award')

    def quota(self, profile, *, require=False):
        level = profile.get('buildings',{}).get(str(BUILDING),0)
        if isinstance(level,dict): level = level.get('level',0)
        limit = self.quotas.get(number(level,0,5),0)
        worker = profile.get('oracle_content',{}).get('work',{}).get(str(BUILDING),{})
        owned = {r['uid']:r['id'] for r in profile.get('gospels',[])}
        uid = worker.get('gospel_uid')
        if not limit or worker.get('is_valid') != 1 or uid not in owned or worker.get('gospel_id') != owned[uid]:
            if require: raise ValueError('赛马需要已启用娱乐室及入驻星痕。')
            return 0
        return limit

    def add_coin(self, profile, amount):
        inventory = normalize_inventory(profile.get('inventory',{}))
        row = inventory.setdefault(str(COIN),dict(id=COIN,uid=stable_item_uid(COIN),count=0,para=0))
        if row['count']+amount > self.maximum: raise EconomyError('圣殿币空间不足，赛果保留；腾出空间后重新进入赛马。',429)
        row['count'] += amount
        return inventory

    def course(self, r):
        def draw(label, modulus): return int(hashlib.sha256((r['seed']+label).encode()).hexdigest()[:16],16)%modulus
        lanes = []
        horses = []
        for i,h in enumerate(HORSES,1):
            sections = [{'ObjectType':draw(f'{i}:{n}:object',4),'IsBlock':bool(draw(f'{i}:{n}:block',2)),
                         'SkillValue':min(100,8*(n+1))} for n in range(17)]
            lanes.append({'Id':i,'SectionList':sections})
            others = [v for v in HORSES if v != h]
            horses.append({'Id':h,'Status':2,'RaceWayId':i,'InitSpeed':(50+draw(f'{i}:speed',16))/1000,
                           'SpeedLvl':6,'LuckLvl':6,'Profits':self.profits,'TargetHorseId':others[draw(f'{i}:target',3)]})
        return {'HorseId':r['horse'],'BetNum':r['units'],'RaceWayList':lanes,'HorseList':horses}

    def history(self, profile, r, operation, amount, *, reward=False):
        entry = {'source':'race','command_id':486 if reward else (484 if operation=='bet' else 482),
                 'operation':operation,'round_id':r['id'],'costs':[],'rewards':[]}
        resources = [{'type':4,'id':COIN,'num':amount}]
        entry['costs' if operation=='bet' else 'rewards'] = resources
        profile['resource_history'] = [*copy.deepcopy(profile.get('resource_history',[])),entry]
        if reward:
            claim = 'race:'+r['id']
            history = copy.deepcopy(profile.get('reward_history',[]))
            if any(v.get('claim_key')==claim for v in history): raise ValueError('race reward already claimed')
            history.append(dict(claim_key=claim,source='race',command_id=486,rewards=resources))
            profile['reward_history'] = history

    def bet(self, p, s, now, spec, operation):
        limit = self.quota(p,require=True)
        if s['active'] is not None: raise ValueError('上局尚未处理，请重新进入赛马页。')
        if spec['units'] > min(limit,s['remaining']): raise ValueError('今日赛马份额不足。')
        stake = self.stake*spec['units']
        changes = deduct_resources(p,[(4,COIN,stake)])
        if changes['inventory'][str(COIN)]['count'] + self.profits[0]*spec['units'] > self.maximum:
            raise EconomyError('请先为本局最高奖励留出圣殿币空间。',429)
        s['sequence'] += 1
        r = {'id':hashlib.sha256(f"race:1:{p['role_id']}:{s['sequence']}:{operation}".encode()).hexdigest()[:32],
             'seed':hashlib.sha256(f"course:1:{p['role_id']}:{s['rng_counter']}".encode()).hexdigest()[:32],
             'horse':spec['horse'],'units':spec['units'],'stake':stake,'started':now,'day':s['day'],
             'ranks':None,'status':'running','award':0}
        s['remaining'] -= r['units']; s['active'] = r
        p.update(changes); self.history(p,r,'bet',stake)
        return RaceResult(485,self.codec.encode('SC_BetRaceHorseRet',self.course(r))+encode_bytes(126,r['id'].encode()))

    def pay(self, p, s):
        r = s['active']
        rank = next(v['Rank'] for v in r['ranks'] if v['HorseId']==r['horse'])
        amount = self.profits[rank-1]*r['units']
        inventory = self.add_coin(p,amount)
        self.history(p,r,'settle',amount,reward=True)
        p['inventory'] = inventory
        r.update(status='settled',award=amount)
        s.update(active=None,last=r,rng_counter=s['rng_counter']+1)
        return RaceResult(487,self.codec.encode('SC_SettleRaceHorseRet',{'HorseRankList':r['ranks']})+encode_bytes(126,r['id'].encode()))

    def settle(self, p, s, now, spec):
        r = s['active']
        if r is None or r['id'] != spec['round_id']: raise ValueError('赛果不属于当前未结算局。')
        if now-r['started'] < 5: raise ValueError('赛马尚未完成。')
        if r['ranks'] is not None and r['ranks'] != spec['ranks']: raise ValueError('已保存赛果不可更换。')
        r.update(ranks=spec['ranks'],status='pending')
        try: return self.pay(p,s)
        except EconomyError as exc: return self.error(486,str(exc),exc.error_code,r['id'])

    def cancel_prepared_bets(self, p):
        for key,value in p.get('client_operation_intents',{}).items():
            if f'.{p["role_id"]}.484.' not in key: continue
            operation,encoded = value.split('|')
            raw = base64.b64decode(encoded,validate=True)+encode_bytes(127,token(operation).encode())
            identity,receipt = mutation_receipts.find(p,484,raw)
            if receipt is None:
                mutation_receipts.commit(p,484,identity,self.error(484,'重进页面已取消尚未扣款的投注。'))

    def recover(self, p, s, now):
        r = s['active']
        # A durable finish intent wins over cancellation, including a process
        # death after JNI prepare but before the protocol reached dispatch.
        if r is not None and r['ranks'] is None:
            for key,value in p.get('client_operation_intents',{}).items():
                if key != f'mte.pendingOperations.{p["role_id"]}.486.1': continue
                operation,encoded = value.split('|')
                raw = base64.b64decode(encoded,validate=True)+encode_bytes(127,token(operation).encode())
                spec = self.parse(486,raw)
                if spec['round_id'] != r['id']: continue
                identity,receipt = mutation_receipts.find(p,486,raw)
                if receipt is None:
                    result = self.settle(p,s,now,spec)
                    mutation_receipts.commit(p,486,identity,result)
                break
        r = s['active']
        if r is not None:
            if r['ranks'] is not None:
                try: self.pay(p,s)
                except EconomyError as exc: return str(exc)
            else:
                try: inventory = self.add_coin(p,r['stake'])
                except EconomyError: return '上局未完成，圣殿币空间不足；退款已保留，请腾出空间后重进。'
                p['inventory'] = inventory
                self.history(p,r,'refund',r['stake'])
                if r['day']==s['day']: s['remaining'] = min(30,s['remaining']+r['units'])
                r['status']='refunded'; s.update(active=None,last=r)
        self.cancel_prepared_bets(p)
        last = s['last']
        if last is None: return ''
        if last['status']=='refunded': return f"上局未完成，已退回{last['stake']}圣殿币；仅当日恢复份额。"
        place = next(v['Rank'] for v in last['ranks'] if v['HorseId']==last['horse'])
        return f"上局第{place}名，{last['award']}圣殿币已到账（总返还额）。"

    def current_notifications(self, profile):
        row = profile.get('inventory',{}).get(str(COIN))
        return ((26,make_update_item(profile['role_id'],row,self.codec)),) if row else ()

    def replay(self, profile, command, result):
        s,_ = self.state(profile)
        rid = scalar(fields(result.body,{1:0,2:0,4:2,5:2,6:0,126:2,127:2},(4,5)),126) if command==484 else None
        if command==484 and result.ok and (s['active'] is None or s['active']['id'].encode()!=rid):
            failure = self.error(484,'这局已结束或退款，请重新选马。')
            operation = scalar(fields(result.body,{1:0,2:0,4:2,5:2,6:0,126:2,127:2},(4,5)),127)
            return replace(result,body=failure.body+encode_bytes(127,operation),error_code=failure.error_code,
                           notifications=self.current_notifications(profile))
        return replace(result,notifications=self.current_notifications(profile))

    def handle(self, command, profile, body=b''):
        if command not in RESPONSE_IDS: return None
        try:
            p = copy.deepcopy(profile)
            s,now = self.state(p)
            if command==482:
                if body: raise ValueError('race query must be empty')
                notice = self.recover(p,s,now)
                horses = [{'Id':h,'Status':2,'SpeedLvl':6,'LuckLvl':6,'Profits':self.profits} for h in HORSES]
                values = {'HorseList':horses,'TodayBetRaceHorseTimes':min(self.quota(p),s['remaining'])}
                result = RaceResult(483,self.codec.encode('SC_RequestRaceHorseDatasRet',values)+(encode_bytes(125,notice.encode()) if notice else b''))
            else:
                spec = self.parse(command,body)
                result = self.bet(p,s,now,spec,string(fields(body,{1:0,2:0,3:0,127:2}),127)) if command==484 else self.settle(p,s,now,spec)
            p[KEY]=s
            return replace(result,changes={k:v for k,v in p.items() if profile.get(k)!=v})
        except (ValueError,KeyError,TypeError,OverflowError,struct.error) as exc:
            if command==482:
                # Query has no Errorcode. Fail closed with no selectable horse,
                # no quota and a visible recovery diagnostic, never fake success.
                return RaceResult(483,encode_bytes(125,('赛马状态未通过校验：'+str(exc)).encode()),error_code=32769,reason=str(exc))
            return self.error(command,str(exc),getattr(exc,'error_code',32769))
