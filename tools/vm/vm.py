#!/usr/bin/env python3
"""Disposable QEMU guests; no host disks, credentials, bridge, or production mounts."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import sys
import subprocess
from pathlib import Path


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout.strip()


def prepare(directory: Path, image: Path, sha256: str, port: int, memory: int):
    image=image.resolve(); directory=directory.resolve()
    if directory.exists():
        raise ValueError('Choose a new guest directory; existing guests are never overwritten')
    digest=hashlib.file_digest(image.open('rb'), 'sha256').hexdigest()
    if digest != sha256:
        raise ValueError('Cloud-image SHA256 differs from the explicitly verified value')
    directory.mkdir(parents=True, mode=0o700)
    run('ssh-keygen','-q','-t','ed25519','-N','','-f',str(directory/'id_ed25519'))
    key=(directory/'id_ed25519.pub').read_text().strip()
    (directory/'user-data').write_text('#cloud-config\nhostname: install-test\nmanage_etc_hosts: true\nusers:\n  - name: tester\n    shell: /bin/bash\n    sudo: ALL=(ALL) NOPASSWD:ALL\n    lock_passwd: true\n    ssh_authorized_keys:\n      - '+key+'\nssh_pwauth: false\ndisable_root: true\n')
    (directory/'meta-data').write_text('instance-id: '+directory.name+'\nlocal-hostname: install-test\n')
    run('cloud-localds',str(directory/'seed.img'),str(directory/'user-data'),str(directory/'meta-data'))
    run('qemu-img','create','-f','qcow2','-F','qcow2','-b',str(image),str(directory/'os.qcow2'),'32G')
    for name in ('data','backup'):
        run('qemu-img','create','-f','qcow2',str(directory/(name+'.qcow2')),'12G')
    config={'schema_version':1,'image':str(image),'image_sha256':digest,'ssh_port':port,'memory_mb':memory,'acceleration':'kvm' if os.access('/dev/kvm',os.R_OK|os.W_OK) else 'tcg'}
    (directory/'vm.json').write_text(json.dumps(config,indent=2)+'\n')
    return config


def start(directory: Path):
    config=json.loads((directory/'vm.json').read_text())
    if (directory/'qemu.pid').exists():
        pid=int((directory/'qemu.pid').read_text())
        try: os.kill(pid,0)
        except ProcessLookupError: (directory/'qemu.pid').unlink()
        else: raise ValueError('Guest process already exists; inspect it before restarting')
    args=['qemu-system-x86_64','-machine','q35,accel='+config['acceleration'],'-cpu','host' if config['acceleration']=='kvm' else 'max','-smp','2','-m',str(config['memory_mb']),'-display','none','-daemonize','-pidfile',str(directory/'qemu.pid'),'-serial','file:'+str(directory/'serial.log'),'-monitor','unix:'+str(directory/'monitor.sock')+',server,nowait','-nic',f'user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:{config["ssh_port"]}-:22']
    for name in ('os','data','backup'):
        args.extend(['-drive',f'file={directory/(name+".qcow2")},if=virtio,format=qcow2'])
    args.extend(['-drive',f'file={directory/"seed.img"},if=virtio,format=raw,readonly=on'])
    run(*args)
    return config


def ssh_args(directory: Path):
    config=json.loads((directory/'vm.json').read_text())
    return ['ssh','-i',str(directory/'id_ed25519'),'-p',str(config['ssh_port']),'-o','BatchMode=yes','-o','StrictHostKeyChecking=accept-new','-o','UserKnownHostsFile='+str(directory/'known_hosts'),'-o','ConnectTimeout=5','tester@127.0.0.1']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','start','ssh','stop'])
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--image',type=Path); parser.add_argument('--sha256')
    parser.add_argument('--port',type=int,default=22221); parser.add_argument('--memory',type=int,default=4096)
    raw=sys.argv[1:]
    boundary=raw.index('--') if '--' in raw else len(raw)
    command=raw[boundary+1:]
    args=parser.parse_args(raw[:boundary]); directory=args.directory.resolve()
    if args.action=='prepare':
        if not args.image or not args.sha256: parser.error('prepare needs --image and --sha256')
        print(json.dumps(prepare(directory,args.image,args.sha256,args.port,args.memory),indent=2))
    elif args.action=='start': print(json.dumps(start(directory),indent=2))
    elif args.action=='ssh':
        raise SystemExit(subprocess.call(ssh_args(directory)+command))
    elif args.action=='stop':
        raise SystemExit(subprocess.call(ssh_args(directory)+['sudo','poweroff']))

if __name__=='__main__': main()
