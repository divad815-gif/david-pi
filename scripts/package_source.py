#!/usr/bin/env python3
"""Build a reproducibly ordered source archive from the screened public file set."""
import argparse
import gzip
import io
import tarfile
from pathlib import Path
from stable_release_gate import ROOT, source_files


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True); args=parser.parse_args()
    version=(ROOT/'VERSION').read_text().strip()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', mtime=0, filename='') as zipped, tarfile.open(fileobj=zipped, mode='w') as archive:
        for relative in source_files(ROOT):
            payload=(ROOT/relative).read_bytes()
            info=tarfile.TarInfo(f'david-pi-{version}/{relative.as_posix()}')
            info.size=len(payload); info.mode=0o755 if (ROOT/relative).stat().st_mode & 0o111 else 0o644
            info.mtime=1577836800; info.uid=info.gid=0; info.uname=info.gname=''
            archive.addfile(info, io.BytesIO(payload))
    print(args.output)

if __name__=='__main__': main()
