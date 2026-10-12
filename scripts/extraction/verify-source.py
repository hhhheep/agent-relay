from pathlib import Path
import hashlib,json,os,sys
ROOT=Path(__file__).resolve().parents[2]
data=json.loads((ROOT/'docs/extraction/source-map.json').read_text(encoding='utf-8'))
bad=[r['target'] for r in data['files'] if not (ROOT/r['target']).is_file() or hashlib.sha256((ROOT/r['target']).read_bytes()).hexdigest()!=r['target_sha256']]
escaped=[];junctions=0
for base,dirs,files in os.walk(ROOT,followlinks=False):
 for name in list(dirs):
  p=Path(base)/name
  if p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()):
   junctions+=1;dirs.remove(name)
   if ROOT.resolve() not in p.resolve().parents:escaped.append(str(p.relative_to(ROOT)))
 for name in files:
  p=Path(base)/name
  if p.is_symlink() and ROOT.resolve() not in p.resolve().parents:escaped.append(str(p.relative_to(ROOT)))
print(json.dumps({'source_files_checked':len(data['files']),'source_mismatches':bad,'internal_junctions':junctions,'external_links':escaped}))
raise SystemExit(bool(bad or escaped))
