"""Durable per-account receipts for draw, breakthrough and rune recast intent.

Field 127 is an explicit offline-only UUID echoed in the response. Runtimes
without it keep legacy semantics. A receipt is committed in the same locked
account transaction as all costs, rewards, RNG and progression changes.
"""
from __future__ import annotations
import base64
import copy
import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass,field
try:
    from .gacha import parse_fields,field_bytes,field_varint,DrawOutcome,Reward
except ImportError:
    from gacha import parse_fields,field_bytes,field_varint,DrawOutcome,Reward

COMMANDS={30:31,39:40,218:219,484:485,486:487,622:623,624:625,630:631,947:948,949:950,1092:1093,
          **{c:c+1 for c in (626,628,632,634,636,638,640,642,644)}}
KEY='mutation_receipts'


@dataclass(frozen=True)
class ReplayedResult:
    response_id:int
    body:bytes
    changes:dict=field(default_factory=dict)
    notifications:tuple=()
    post_notifications:tuple=()
    error_code:int=0
    reason:str='durable operation receipt replayed'
    role_id:int=0
    card_pool_id:int=0
    draws:tuple=()
    replayed:bool=True
    @property
    def ok(self):return self.error_code==0


def identity(command,body):
    if command not in COMMANDS:return None
    values=parse_fields(body)
    ids=values.pop(127,[])
    if not ids:return None
    if len(ids)!=1 or not isinstance(ids[0],bytes) or re.fullmatch(rb'[0-9a-f]{32}',ids[0]) is None:
        raise ValueError('Invalid local operation ID')
    def canon(value):return {'bytes':value.hex()} if isinstance(value,bytes) else value
    plain=json.dumps({str(k):[canon(v) for v in rows] for k,rows in sorted(values.items())},sort_keys=True,separators=(',',':'))
    return ids[0].decode('ascii'),hashlib.sha256((str(command)+':'+plain).encode()).hexdigest()


def find(profile,command,body):
    try:key=identity(command,body)
    except ValueError as exc:return None,ReplayedResult(COMMANDS[command],field_varint(1,32000),error_code=32000,reason=str(exc))
    if key is None:return None,None
    token,fingerprint=key
    row=profile.get(KEY,{}).get(token)
    if row is None:return key,None
    if row['command']!=command or row['fingerprint']!=fingerprint:
        return key,ReplayedResult(COMMANDS[command],field_varint(1,32000)+field_bytes(127,token.encode()),error_code=32000,reason='Operation ID reused with different parameters')
    draws=[]
    for source in row.get('draws',[]):
        d=copy.deepcopy(source);d['conversions']=tuple(Reward(**r) for r in d.get('conversions',[]));draws.append(DrawOutcome(**d))
    return key,ReplayedResult(row['response_id'],base64.b64decode(row['body']),error_code=row['error_code'],
        role_id=int(profile.get('role_id',0)),card_pool_id=row.get('card_pool_id',0),draws=tuple(draws))


def commit(profile,command,key,result):
    if key is None or result is None:return result
    token,fingerprint=key
    result=dataclasses.replace(result,body=result.body+field_bytes(127,token.encode()))
    receipts=copy.deepcopy(profile.get(KEY,{}))
    receipts[token]=dict(command=command,fingerprint=fingerprint,response_id=result.response_id,
        body=base64.b64encode(result.body).decode(),error_code=int(result.error_code),
        card_pool_id=int(getattr(result,'card_pool_id',0)),draws=[dataclasses.asdict(d) for d in getattr(result,'draws',())])
    profile[KEY]=receipts
    return result
