#!/usr/bin/env python3
"""Keep only ABIs present in both input archives.

The input archives remain unchanged. Every retained entry is checked
byte-for-byte before the output is accepted.
"""
from __future__ import annotations
import argparse,copy,hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path

GAME_LIBRARIES={'libmain.so','libunity.so','libil2cpp.so'}


def abi_libraries(names):
    result={}
    for name in names:
        parts=name.split('/')
        if len(parts)==3 and parts[0]=='lib' and parts[2].endswith('.so'):
            result.setdefault(parts[1],set()).add(parts[2])
    return result


def compatible_abis(base_names,helper_names):
    game={abi for abi,libs in abi_libraries(base_names).items() if GAME_LIBRARIES<=libs}
    backend={abi for abi,libs in abi_libraries(helper_names).items()
             if 'libchaquopy_java.so' in libs and any(re.fullmatch(r'libpython\d+\.\d+\.so',n) for n in libs)}
    result=game&backend
    if not result:raise ValueError('No ABI contains both Unity and the embedded Python runtime')
    return result


def prepare(base,helper,output, *, aapt=None):
    if output.exists() or output.resolve() in (base.resolve(),helper.resolve()):raise ValueError('Choose a new output file')
    sdk={}
    if aapt:
        for name,path in [('base',base),('helper',helper)]:
            badging=subprocess.check_output([str(aapt),'dump','badging',str(path)],text=True)
            match=re.search(r"^sdkVersion:'(\d+)'",badging,re.M)
            if not match:raise ValueError('Could not read '+name+' minimum SDK')
            sdk[name]=int(match[1])
        if sdk['base']<sdk['helper']:
            raise ValueError(f"Base requires API {sdk['base']} but backend requires {sdk['helper']}; rebuild the base with the backend's minimum SDK")
    removed=[]
    with zipfile.ZipFile(base) as source,zipfile.ZipFile(helper) as runtime:
        supported=compatible_abis(source.namelist(),runtime.namelist())
        with zipfile.ZipFile(output,'w',allowZip64=True) as target:
            for info in source.infolist():
                parts=info.filename.split('/')
                if len(parts)>=2 and parts[0]=='lib' and parts[1] and parts[1] not in supported:
                    removed.append(info.filename);continue
                with source.open(info) as old,target.open(copy.copy(info),'w',force_zip64=True) as new:shutil.copyfileobj(old,new)
        with zipfile.ZipFile(output) as verified:
            expected=set(source.namelist())-set(removed)
            if set(verified.namelist())!=expected:raise ValueError('Runtime base changed unrelated entries')
            for name in expected:
                if source.getinfo(name).CRC!=verified.getinfo(name).CRC or source.getinfo(name).file_size!=verified.getinfo(name).file_size:
                    raise ValueError('Retained entry changed: '+name)
                with source.open(name) as old,verified.open(name) as new:
                    while True:
                        chunk=old.read(1024*1024)
                        if chunk!=new.read(1024*1024):raise ValueError('Retained entry bytes changed: '+name)
                        if not chunk:break
    report={'base':str(base.resolve()),'helper':str(helper.resolve()),'output':str(output.resolve()),
            'supported_abis':sorted(supported),'removed_incomplete_abi_entries':removed,'minimum_sdk':sdk,
            'base_sha256':hashlib.sha256(base.read_bytes()).hexdigest(),'output_sha256':hashlib.sha256(output.read_bytes()).hexdigest()}
    output.with_suffix('.compatibility.json').write_text(json.dumps(report,indent=2))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['base','helper','output']:parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--aapt',type=Path,required=True);args=parser.parse_args()
    print(json.dumps(prepare(args.base,args.helper,args.output,aapt=args.aapt)))
