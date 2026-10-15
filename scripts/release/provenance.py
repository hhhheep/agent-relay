"""Record and verify engineering changes without editing extraction provenance."""
from __future__ import annotations
import argparse, hashlib, json, subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / 'docs/engineering/phase-one/source-changes.json'
BASELINE = 'bf8db034ee0e72967296b916968dfe2460e35cbe'
def changes():
    source = ROOT / 'docs/extraction/source-map.json'
    original = json.loads(source.read_text(encoding='utf-8'))
    entries = []
    for item in original['files']:
        name = item['target']
        path = ROOT / name
        current = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if current != item['target_sha256']:
            entries.append({'path':name,'extraction_sha256':item['target_sha256'],'current_sha256':current})
    return {'schema_version':1,'engineering_baseline':BASELINE,
            'historical_map_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'decisions':'docs/engineering/phase-one/DECISIONS.md','changes':entries}
def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--write',action='store_true');args=parser.parse_args()
    expected=changes()
    historical=subprocess.run(['git','show',f'{BASELINE}:docs/extraction/source-map.json'],cwd=ROOT,capture_output=True,check=True).stdout
    if hashlib.sha256(historical).hexdigest()!=expected['historical_map_sha256']:
        raise SystemExit('Historical source map changed')
    if args.write:
        OUTPUT.parent.mkdir(parents=True,exist_ok=True)
        OUTPUT.write_text(json.dumps(expected,indent=2)+'\n',encoding='utf-8')
    actual=json.loads(OUTPUT.read_text(encoding='utf-8'))
    if actual != expected: raise SystemExit('Engineering provenance is stale; review changes and regenerate')
    print(json.dumps({'historical_map_unchanged':True,'changed_extraction_files':len(expected['changes'])}))
if __name__=='__main__': main()
