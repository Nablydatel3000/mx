"""Read-only file inventory, not a backtest or assertion of data authenticity."""
import argparse
import hashlib
import json
import os
from pathlib import Path


def inventory(roots):
    excluded={'.git','.venv','__pycache__','.aws','.codex','.agents','node_modules'}
    suffixes={'.csv','.db','.sqlite','.sqlite3','.zip','.parquet','.arrow','.jsonl','.ndjson','.gz'}
    candidates=[];snapshots=[];errors=[]
    for root in roots:
        def on_error(error):
            errors.append(dict(path=str(error.filename),category=type(error).__name__))
        for folder,dirs,files in os.walk(root,onerror=on_error,followlinks=False):
            dirs[:]=sorted(d for d in dirs if d not in excluded)
            if 'manifest.json' in files:snapshots.append(str(Path(folder)/'manifest.json'))
            for name in sorted(files):
                path=Path(folder)/name
                if path.suffix.lower() in suffixes:
                    try:candidates.append(dict(path=str(path),bytes=path.stat().st_size))
                    except OSError as error:on_error(error)
    repo=Path(__file__).resolve().parents[2]
    sources={}
    for name in ('strategy.py','research.py','engine.py','history.py','api.py'):
        sources[name]=hashlib.sha256((repo/'src/gldrubf_bot'/name).read_bytes()).hexdigest()
    return dict(schema=1,roots=[str(p) for p in roots],excluded_directories=sorted(excluded),
        follow_directory_symlinks=False,candidates=candidates,snapshot_manifests=sorted(snapshots),
        scan_errors=errors,source_sha256=sources,
        note='Candidate filenames are not proof of genuine historical market data. No performance calculated.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',action='append',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=inventory([p.resolve() for p in args.root])
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(candidates=len(result['candidates']),snapshots=len(result['snapshot_manifests']),
                         scan_errors=len(result['scan_errors']))))
