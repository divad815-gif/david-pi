import importlib.util
import json
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('portable_status',ROOT/'scripts/collect_server_status.py')
collector=importlib.util.module_from_spec(spec);spec.loader.exec_module(collector)
from modules.maintenance_observations import load_server_status, metric_sample


def fixture(tmp_path):
    data=tmp_path/'data';data.mkdir()
    instance=str(uuid.uuid4());(data/'.david-pi-storage').write_text(instance)
    return {'instance_id':instance,'public_url':'https://server.example-tail.ts.net','storage':{'data_root':str(data),'backup_root':None},'modules':{'chat':'disabled','pihole':'disabled'},'integrations':{'web_push':False},'members':[{'role':'admin'}]}


def runner(args):
    if args[0]=='docker' and 'ps' in args:
        return json.dumps([{'Service':'portal','ID':'a'*64,'State':'running','Health':'healthy'},{'Service':'maintenance','ID':'b'*64,'State':'running'}])
    if args[0]=='docker' and 'stats' in args:return json.dumps({'CPUPerc':'1.2%','MemPerc':'3.4%'})
    if args[:2]==['tailscale','status']:return '{"BackendState":"Running"}'
    if args[:3]==['tailscale','serve','status']:
        return json.dumps({'TCP':{'443':{'HTTPS':True}},'Web':{'server.example-tail.ts.net:443':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:8090'}}}},'AllowFunnel':{}})
    return None


def test_disabled_modules_are_not_missing_services_and_metrics_accept_document(tmp_path):
    cfg=fixture(tmp_path)
    value=collector.collect(cfg,{'services':{'portal':{},'maintenance':{}}},tmp_path,runner)
    assert value['state']=='healthy'
    assert value['subsystems']['pihole']['state']=='disabled'
    assert value['subsystems']['backups']['state']=='not_configured'
    assert value['subsystems']['background_jobs']['details']['browser_push']=='disabled'
    text=json.dumps(value)
    assert cfg['instance_id'] not in text and cfg['public_url'] not in text and str(tmp_path) not in text
    target=tmp_path/'status.json';collector.write(target,value)
    assert metric_sample(load_server_status(target))['cpu']==1.2


def test_enabled_worker_missing_and_storage_missing_are_truthful(tmp_path):
    cfg=fixture(tmp_path)
    value=collector.collect(cfg,{'services':{'portal':{},'maintenance':{},'slideshow':{}}},tmp_path,runner)
    assert value['subsystems']['services']['details']['missing_count']==1
    (Path(cfg['storage']['data_root'])/'.david-pi-storage').unlink()
    value=collector.collect(cfg,{'services':{'portal':{},'maintenance':{}}},tmp_path,runner)
    assert value['subsystems']['external_drive']['state']=='critical'
    assert value['subsystems']['storage']['state']=='unavailable'


def test_funnel_or_wrong_private_mapping_is_not_healthy(tmp_path):
    cfg=fixture(tmp_path)
    def unsafe(args):
        if args[:3]==['tailscale','serve','status']:
            return json.dumps({'TCP':{'443':{'HTTPS':True}},'AllowFunnel':{'host:443':True}})
        return runner(args)
    value=collector.collect(cfg,{'services':{'portal':{},'maintenance':{}}},tmp_path,unsafe)
    assert value['subsystems']['tailscale']['state']=='critical'
