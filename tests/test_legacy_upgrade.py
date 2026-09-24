"""Migration regression with real historical schemas and synthetic content only."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import uuid
import pytest

ROOT=Path(__file__).resolve().parents[1]
OLD_TAG='v9.22.2'
OLD_COMMIT='cc47ff7eb5210d3379fc1ab07e0ce7e11f166986'


def run_probe(mode,source,data,manifest,report,config=None):
    command=[sys.executable,str(ROOT/'tests/legacy_migration_probe.py'),mode,'--source',str(source),'--data',str(data),'--manifest',str(manifest),'--report',str(report)]
    if config:command.extend(['--config',str(config)])
    result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stderr+result.stdout
    return json.loads(report.read_text())


def run_upgrade_fixture(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    revision=subprocess.run(['git','rev-parse',f'{OLD_TAG}^{{commit}}'],cwd=ROOT,capture_output=True,text=True)
    if revision.returncode:
        pytest.skip('Historical v9.22.2 tag is absent; fetch full repository history for the migration gate')
    assert revision.stdout.strip()==OLD_COMMIT,'The historical migration source changed'
    source=directory/'source';source.mkdir();archive=directory/'source.tar'
    subprocess.run(['git','archive',OLD_COMMIT,'--output',str(archive)],cwd=ROOT,check=True)
    with tarfile.open(archive) as bundle:bundle.extractall(source,filter='data')
    original=directory/'legacy-data';manifest=directory/'manifest.json'
    seed=run_probe('seed',source,original,manifest,directory/'seed-report.json')
    assert seed['seeded_with_version']=='9.22.2'
    # Migration runs against a copy: the actual old fixture remains recoverable.
    original_hashes={str(p.relative_to(original)):hashlib.sha256(p.read_bytes()).hexdigest() for p in original.rglob('*') if p.is_file()}
    upgraded=directory/'upgraded-data';shutil.copytree(original,upgraded)
    instance=str(uuid.uuid4())
    cfg={'schema_version':1,'instance_id':instance,'display_name':'John Pi fixture','hostname':'john-pi','public_url':'https://john-pi.example.ts.net','timezone':'UTC','country':'US','members':[{'login':name+'@example.test','name':name.title(),'role':'admin' if name=='alice' else 'household'} for name in ('alice','bob','carol')],'storage':{'mode':'folder','data_root':str(upgraded),'backup_root':None},'modules':{name:('manual' if name in ('movies','recipes') else 'disabled' if name=='pihole' else 'enabled') for name in ('media','files','notes','movies','recipes','places','audiobooks','mytube','chat','games','assistant','device_backup','pihole')},'integrations':{'web_push':False}}
    config=directory/'installation.json';config.write_text(json.dumps(cfg,indent=2));(upgraded/'.david-pi-storage').write_text(instance+'\n')
    first=run_probe('verify',ROOT,upgraded,manifest,directory/'upgrade-first-report.json',config)
    second=run_probe('verify',ROOT,upgraded,manifest,directory/'upgrade-second-report.json',config)
    assert first==second,'Repeated startup changed migrated schema or fixture content'
    assert original_hashes=={str(p.relative_to(original)):hashlib.sha256(p.read_bytes()).hexdigest() for p in original.rglob('*') if p.is_file()},'Original recovery fixture was changed'
    result={'historical_tag':OLD_TAG,'historical_commit':OLD_COMMIT,'seed':seed,'upgrade':second,'repeat_idempotent':True,'original_fixture_unchanged':True,'production_data_used':False}
    (directory/'acceptance-report.json').write_text(json.dumps(result,indent=2))
    return result


def test_real_v922_upgrade_preserves_content_privacy_and_idempotency(tmp_path):
    result=run_upgrade_fixture(tmp_path)
    assert result['upgrade']['counts']=={'photos':2,'notes':3,'stored_files':2,'recipes':1,'movies':2,'messages':1}


if __name__=='__main__':
    destination=Path(sys.argv[1]).resolve()
    print(json.dumps(run_upgrade_fixture(destination),indent=2))
