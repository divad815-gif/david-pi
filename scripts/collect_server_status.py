#!/usr/bin/env python3
"""Portable, selected-service health collector; never emits private config or content."""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from modules.installation import validate_installation
from modules.maintenance_observations import EXPECTED_PRIVACY


def command(args, timeout=12):
    try:
        result=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
        return result.stdout[:1048576] if result.returncode==0 else None
    except (OSError,subprocess.TimeoutExpired):
        return None


def read_json(path, default=None):
    try:
        if path.is_symlink() or path.stat().st_size>1048576: return default
        return json.loads(path.read_text())
    except (OSError,ValueError): return default


def rows(raw):
    if raw is None: return []
    try:
        value=json.loads(raw)
        return value if isinstance(value,list) else [value]
    except ValueError:
        try: return [json.loads(line) for line in raw.splitlines() if line.strip()]
        except ValueError: return []


def numeric(value):
    try:
        number=float(str(value).rstrip('%'))
        return number if math.isfinite(number) else None
    except (TypeError,ValueError): return None


def card(state,summary,details=None,action=''):
    return {'state':state,'summary':summary,'details':details or {},'recommended_action':action,'evidence_code':state.upper(),'updated_at':datetime.now(timezone.utc).isoformat()}


def disk_usage(path):
    result=os.statvfs(path)
    total=result.f_blocks*result.f_frsize
    free=result.f_bavail*result.f_frsize
    return {'total_gb':round(total/(1024**3),2),'free_gb':round(free/(1024**3),2),'used_gb':round((total-free)/(1024**3),2),'used_percent':round((total-free)*100/max(total,1),1)}


def selected_services(compose, process_rows):
    expected=set(compose.get('services',{}))
    observed={r.get('Service'):r for r in process_rows if isinstance(r,dict)}
    missing=[];unhealthy=[];running=0
    for name in expected:
        row=observed.get(name,{})
        if row.get('State')!='running': missing.append(name)
        elif row.get('Health','') not in ('','healthy'): unhealthy.append(name)
        else: running+=1
    return {'selected_count':len(expected),'running_count':running,'missing_count':len(missing),'unhealthy_count':len(unhealthy)}


def collect(cfg, compose, etc, run=command, now=None):
    generated=datetime.fromtimestamp(time.time() if now is None else now,timezone.utc).isoformat()
    process_rows=rows(run(['docker','compose','--project-name','david-pi','-f',str(etc/'compose.json'),'ps','--all','--format','json']))
    inventory=selected_services(compose,process_rows)
    services_ok=inventory['selected_count']>0 and inventory['running_count']==inventory['selected_count']
    subsystem={'services':card('healthy' if services_ok else 'warning','Selected services are running.' if services_ok else 'One or more selected services need attention.',inventory)}
    portal=next((p for p in process_rows if p.get('Service')=='portal'),{})
    identifier=portal.get('ID','')
    stats=rows(run(['docker','stats','--no-stream','--format','json',identifier])) if re.fullmatch(r'[a-f0-9]{12,64}',identifier) else []
    cpu=numeric(stats[0].get('CPUPerc')) if stats else None
    memory=numeric(stats[0].get('MemPerc')) if stats else None
    healthy=portal.get('State')=='running' and portal.get('Health','') in ('','healthy')
    subsystem['portal']=card('healthy' if healthy else 'critical','Portal is running.' if healthy else 'Portal is unavailable.',{'cpu_percent':cpu,'memory_percent':memory})
    data=Path(cfg['storage']['data_root'])
    try:
        sentinel=data/'.david-pi-storage'
        if sentinel.is_symlink() or sentinel.read_text().strip()!=cfg['instance_id']: raise ValueError('storage identity')
        usage=disk_usage(data)
        subsystem['external_drive']=card('healthy','Configured storage identity matches.',{'mounted':True})
        subsystem['storage']=card('critical' if usage['used_percent']>=95 else 'warning' if usage['used_percent']>=85 else 'healthy','Primary storage capacity.',{'external':usage,'microsd':disk_usage('/')})
    except (OSError,ValueError):
        subsystem['external_drive']=card('critical','Required storage is missing or its identity differs.',{'mounted':False})
        subsystem['storage']=card('unavailable','Storage capacity cannot be verified.',{'external':{}})
    mem={}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            key,value=line.split(':',1);mem[key]=int(value.strip().split()[0])
        details={'ram_total_gb':round(mem.get('MemTotal',0)/1048576,3),'ram_available_gb':round(mem.get('MemAvailable',0)/1048576,3),'swap_total_gb':round(mem.get('SwapTotal',0)/1048576,3),'swap_free_gb':round(mem.get('SwapFree',0)/1048576,3),'load_average':list(os.getloadavg()),'host_uptime_seconds':int(float(Path('/proc/uptime').read_text().split()[0])),'temperature_c':None}
        subsystem['temperature_power']=card('healthy','Host memory and load observations available; hardware-specific temperature unavailable.',details)
    except (OSError,ValueError): subsystem['temperature_power']=card('unavailable','Host resource observations unavailable.')
    backup_root=cfg['storage'].get('backup_root')
    backup=read_json(etc/'host-state/backup-status.json',{})
    if not backup_root:
        subsystem['backups']=card('not_configured','Independent backup is not configured.',{'configured':False,'restore_verified':False})
    else:
        checked=backup.get('state')=='integrity_verified'
        subsystem['backups']=card('warning','Backup integrity checked; clean restore remains unverified.' if checked else 'Independent backup selected; clean restore remains unverified.',{'configured':True,'integrity_verified':checked,'restore_verified':False})
    enabled=cfg['modules'].get('pihole')!='disabled'
    if not enabled:
        subsystem['pihole']=card('disabled','Pi-hole is disabled.',{'enabled':False})
    else:
        summary=read_json(Path('/run/david-pi/pihole-summary.json'),{})
        stamp=summary.get('updated_at') or summary.get('generated_at')
        try: fresh=0 <= time.time()-datetime.fromisoformat(str(stamp).replace('Z','+00:00')).timestamp()<=900
        except (TypeError,ValueError): fresh=False
        subsystem['pihole']=card('healthy' if fresh and summary.get('enabled') is True and not summary.get('stale') else 'unavailable','Pi-hole aggregate status available.' if fresh else 'Pi-hole is selected but a current aggregate observation is unavailable.',{'enabled':True,'observation_fresh':fresh})
    push=cfg['modules'].get('chat')!='disabled' and cfg['integrations'].get('web_push') is True
    worker_count=max(0,inventory['selected_count']-1)
    subsystem['background_jobs']=card('healthy' if services_ok else 'warning','Only selected module workers are monitored.',{'selected_workers':worker_count,'browser_push':'enabled' if push else 'disabled','slideshows':{'active':None,'pending':None,'observation':'not_collected'}})
    tail=rows(run(['tailscale','status','--json']))
    connected=bool(tail and tail[0].get('BackendState')=='Running')
    serve=rows(run(['tailscale','serve','status','--json']))
    private=False
    if serve:
        state=serve[0]
        from urllib.parse import urlsplit
        dns=urlsplit(cfg['public_url']).hostname
        proxy=state.get('Web',{}).get(f'{dns}:443',{}).get('Handlers',{}).get('/',{}).get('Proxy')
        private=bool(state.get('TCP',{}).get('443',{}).get('HTTPS') and proxy=='http://127.0.0.1:8090' and not any(state.get('AllowFunnel',{}).values()))
    subsystem['tailscale']=card('healthy' if connected and private else 'critical','Private Tailscale access is verified.' if connected and private else 'Tailscale connection or private HTTPS mapping needs attention.',{'connected':connected,'serve_private':private})
    subsystem['access_control']=card('healthy','Explicit household membership is configured.',{'member_count':len(cfg['members']),'administrator_count':sum(m['role']=='admin' for m in cfg['members'])})
    subsystem['updates']=card('healthy','Updates start only when an administrator requests them.',{'automatic':False})
    # Disabled/not-configured optional capabilities never degrade core health.
    states=[c['state'] for c in subsystem.values()]
    overall='critical' if 'critical' in states else 'warning' if any(s in {'warning','unavailable'} for s in states) else 'healthy'
    return {'schema_version':1,'generated_at':generated,'state':overall,'privacy':dict(EXPECTED_PRIVACY),'subsystems':subsystem,'databases':[]}


def write(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,name=tempfile.mkstemp(prefix='.status-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'w') as stream:
            json.dump(payload,stream,separators=(',',':'),sort_keys=True);stream.write('\n');stream.flush();os.fsync(stream.fileno());os.fchmod(stream.fileno(),0o644)
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--etc',type=Path,default=Path('/etc/david-pi'));parser.add_argument('--output',type=Path,default=Path('/run/david-pi/server-status.json'));args=parser.parse_args()
    cfg=validate_installation(read_json(args.etc/'installation.json'))
    compose=read_json(args.etc/'compose.json',{})
    write(args.output,collect(cfg,compose,args.etc))

if __name__=='__main__': main()
