import copy
import importlib.util
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stable_gate', ROOT/'scripts/stable_release_gate.py')
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)


def fixture(tmp_path):
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    (tmp_path/'VERSION').write_text('10.0.0\n')
    digest = gate.source_digest(tmp_path)
    return {'schema_version':1,'version':'10.0.0','source_sha256':digest,'reviewer':'Release tester','reviewed_at':'2026-09-23T00:00:00Z','checks':[{'id':name,'environment':environment,'status':'pass','source_sha256':digest,'performed_by':'Fixture tester','completed_at':'2026-09-23T00:00:00Z','notes':'Synthetic unit fixture only','evidence_sha256':'a'*64,'simulated':False} for name,environment in gate.REQUIRED.items()]}


def test_missing_skipped_and_simulated_receipts_fail_closed(tmp_path):
    document = fixture(tmp_path)
    assert gate.validate(document,tmp_path,require_android=False)==[]
    for field,value in [('status','skipped'),('environment','container'),('simulated',True),('evidence_sha256','')]:
        changed=copy.deepcopy(document); changed['checks'][0][field]=value
        assert gate.validate(changed,tmp_path,require_android=False)
    document['checks'].pop()
    assert any('newcomer' in error for error in gate.validate(document,tmp_path,require_android=False))


def test_source_edits_and_wrong_apk_invalidate_receipts(tmp_path):
    document=fixture(tmp_path)
    (tmp_path/'app.py').write_text('changed=True\n')
    assert any('source' in error for error in gate.validate(document,tmp_path,require_android=False))
    assert any('Android' in error for error in gate.validate(document,tmp_path))


def test_private_generated_files_do_not_enter_package(tmp_path):
    fixture(tmp_path)
    for name in ['work/vm/id_ed25519','build/token.txt','.env','artifacts/android/test.apk']:
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('private fixture')
    assert gate.source_files(tmp_path)==[Path('VERSION')]
