"""Real v9.22.2 fixture seed/upgrade probe, executed in isolated subprocesses.

This file contains synthetic inputs only. Old application code is obtained by
`git archive` into a disposable work directory, never copied into this source.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import importlib
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import sys


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=['seed','verify'])
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args();os.umask(0o022);data=args.data.resolve();data.mkdir(parents=True,exist_ok=True,mode=0o750)
    for key in ('TMDB_API_READ_TOKEN','THEMEALDB_API_KEY','GIPHY_API_KEY','DAVID_PI_CONFIG_FILE'):
        os.environ.pop(key,None)
    values={'PHOTO_DATA':str(data),'DAVID_PI_PLATFORM_DATA':str(data/'platform'),'DAVID_PI_FILES_DATA':str(data/'files'),'DAVID_PI_CHAT_DATA':str(data/'chat'),'DAVID_PI_AUDIOBOOKS_DATA':str(data/'audiobooks'),'DAVID_PI_MYTUBE_DATA':str(data/'mytube'),'DAVID_PI_DISABLE_METRICS':'1','DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING':'1','DAVID_PI_CHAT_KEY_B64':base64.b64encode(bytes(range(32))).decode(),'DAVID_PI_CHAT_KEY_FILE':str(data/'fixture-chat.key'),'DAVID_PI_ALLOWED_HOSTS':'localhost,test.localhost','DAVID_PI_ACCESS_MODE':'enforce'}
    os.environ.update(values)
    (data/'fixture-chat.key').write_bytes(bytes(range(32)))
    if args.config:os.environ['DAVID_PI_CONFIG_FILE']=str(args.config.resolve())
    sys.path.insert(0,str(args.source.resolve()))
    portal=importlib.import_module('app');portal.app.config['TESTING']=True
    csrf='synthetic-migration-csrf-with-more-than-32-characters'
    def client(name):
        value=portal.app.test_client();value.environ_base.update(HTTP_X_TEST_TAILSCALE_LOGIN=f'{name}@example.test',HTTP_X_TEST_TAILSCALE_NAME=name.title(),HTTP_TAILSCALE_USER_LOGIN=f'{name}@example.test',HTTP_TAILSCALE_USER_NAME=name.title(),HTTP_X_CSRF_TOKEN=csrf)
        value.set_cookie('david_pi_csrf',csrf,domain='localhost')
        return value
    clients={name:client(name) for name in ('alice','bob','carol','outsider')}
    def checked(response,status=200):
        assert response.status_code==status,(response.status_code,response.get_data(as_text=True)[:400])
        return response.get_json()
    if args.mode=='seed':
        assert not (args.source/'modules/access_control.py').exists(),'Seed must use the real old application'
        manifest={'notes':[],'files':[],'photos':[]}
        for owner,visibility in [('alice','private'),('alice','shared'),('bob','private')]:
            title=f'{owner} {visibility} legacy note';body=f'Synthetic body from v9.22.2: {title}'
            note=checked(clients[owner].post('/api/notes',json={'visibility':visibility}),201)['note']
            note=checked(clients[owner].put('/api/notes/'+note['id'],json={'version':note['version'],'title':title,'body':body,'visibility':visibility,'note_type':'text'}))['note']
            manifest['notes'].append({'id':note['id'],'owner':owner,'visibility':visibility,'title':title,'body':body})
        for visibility in ('private','shared'):
            content=f'Preserved v9.22.2 {visibility} file bytes'.encode();name=f'legacy-{visibility}.txt'
            checked(clients['alice'].post('/api/files/upload',data={'visibility':visibility,'files':(BytesIO(content),name)},content_type='multipart/form-data'),201)
            with sqlite3.connect(data/'platform/files.db') as db:
                identifier=db.execute('SELECT id FROM stored_files WHERE name=?',(name,)).fetchone()[0]
            manifest['files'].append({'id':identifier,'visibility':visibility,'sha256':hashlib.sha256(content).hexdigest()})
        from PIL import Image
        for visibility,color in [('private','blue'),('shared','red')]:
            image=BytesIO();Image.new('RGB',(12,12),color).save(image,'PNG');raw=image.getvalue();image.seek(0)
            value=checked(clients['alice'].post('/api/upload',data={'visibility':visibility,'media':(image,f'legacy-{visibility}.png')},content_type='multipart/form-data'))
            identifier=value['added_items'][0]['id']
            manifest['photos'].append({'id':identifier,'visibility':visibility,'sha256':hashlib.sha256(raw).hexdigest()})
        recipe=checked(clients['alice'].post('/api/recipes',json={'title':'Legacy fixture soup','meal_type':'main','ingredients':['Carrots','Water'],'instructions':['Simmer gently'],'total_minutes':20}),201)['recipe']
        manifest['recipe_id']=recipe['id']
        for media_type in ('movie','tv'):
            checked(clients['alice'].post('/api/movies',json={'title':f'Legacy fixture {media_type}','media_type':media_type,'release_year':2020}),201)
        for name in ('alice','bob','carol'):checked(clients[name].get('/api/chat/users'))
        conversation=checked(clients['alice'].post('/api/chat/conversations',json={'member_ids':['bob@example.test']}),201)['id']
        message=checked(clients['alice'].post(f'/api/chat/conversations/{conversation}/messages',data={'body':'Legacy encrypted fixture message','client_message_id':'synthetic-legacy-message'}),201)['message']
        manifest['conversation_id']=conversation;manifest['message_id']=message['id']
        assert b'Legacy encrypted fixture message' not in (data/'platform/chat.db').read_bytes()
        args.manifest.write_text(json.dumps(manifest,indent=2))
        report={'seeded_with_version':(args.source/'VERSION').read_text().strip(),'schema_source':'real v9.22.2 import and API writes','notes':3,'files':2,'photos':2,'recipes':1,'movies':2,'encrypted_messages':1}
    else:
        manifest=json.loads(args.manifest.read_text());assert args.config
        for note in manifest['notes']:
            for reader in ('alice','bob','carol'):
                allowed=note['visibility']=='shared' or note['owner']==reader
                response=clients[reader].get('/api/notes/'+note['id'])
                if allowed:
                    value=checked(response)['note'];assert value['title']==note['title'] and value['body']==note['body']
                else:assert response.status_code==404
        for kind,route in [('files','/api/files/{id}/content'),('photos','/media/original/{id}')]:
            for item in manifest[kind]:
                for reader in ('alice','bob','carol'):
                    response=clients[reader].get(route.format(id=item['id']))
                    if item['visibility']=='shared' or reader=='alice':
                        assert response.status_code==200,(kind,reader,response.status_code)
                        assert hashlib.sha256(response.data).hexdigest()==item['sha256']
                    else:assert response.status_code==404
        recipe=checked(clients['bob'].get('/api/recipes/'+manifest['recipe_id']))['recipe']
        assert recipe['title']=='Legacy fixture soup' and 'Carrots' in json.dumps(recipe)
        for media_type in ('movie','tv'):
            movies=checked(clients['bob'].get('/api/movies?type='+media_type))['movies']
            assert len(movies)==1 and movies[0]['title']==f'Legacy fixture {media_type}'
        path=f"/api/chat/conversations/{manifest['conversation_id']}/messages"
        for reader in ('alice','bob'):
            messages=checked(clients[reader].get(path))['messages']
            assert messages[0]['body']=='Legacy encrypted fixture message'
        assert clients['carol'].get(path).status_code==404
        for path in ('/api/notes','/api/files','/api/photos','/api/recipes','/api/movies','/api/chat/conversations'):
            assert clients['outsider'].get(path).status_code==403,(path,clients['outsider'].get(path).status_code)
        counts={};ledgers={};schemas={}
        domains={'photos.db':'photos','platform/notes.db':'notes','platform/files.db':'stored_files','platform/recipes.db':'recipes','platform/movies.db':'movies','platform/chat.db':'messages'}
        for relative,table in domains.items():
            with sqlite3.connect(data/relative) as db:
                assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                counts[table]=db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                schema=db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
                schemas[relative]=hashlib.sha256(json.dumps(schema,sort_keys=True).encode()).hexdigest()
                try:ledgers[relative]=db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0]
                except sqlite3.OperationalError:ledgers[relative]=0
        report={'api_content_and_isolation_verified':True,'file_and_photo_bytes_preserved':True,'chat_decryption_verified':True,'unadmitted_identity_denied':True,'counts':counts,'migration_ledgers':ledgers,'schema_digests':schemas}
    args.report.write_text(json.dumps(report,indent=2));print(json.dumps(report))

if __name__=='__main__':main()
