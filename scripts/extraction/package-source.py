"""Produce a checked source-only ZIP from Git publication candidates."""
from pathlib import Path
import hashlib, json, zipfile
from importlib.util import spec_from_file_location, module_from_spec
ROOT = Path(__file__).resolve().parents[2]
spec = spec_from_file_location('publication_guard', Path(__file__).with_name('check-publication.py'))
guard = module_from_spec(spec); spec.loader.exec_module(guard)
names = guard.publication_files()
report = guard.inspect(names)
if not report['passed']:
    print(json.dumps(report, ensure_ascii=False)); raise SystemExit(1)
output = ROOT / '.verification' / 'agent-relay-source.zip'
output.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
    for name in names: archive.write(ROOT / name, arcname='agent-relay/' + name)
with zipfile.ZipFile(output) as archive:
    archived = [n.removeprefix('agent-relay/') for n in archive.namelist()]
    assert archived == names
    for name in names:
        assert hashlib.sha256(archive.read('agent-relay/' + name)).digest() == hashlib.sha256((ROOT/name).read_bytes()).digest()
report.update(archive='agent-relay-source.zip', archive_files=len(names), sha256=hashlib.sha256(output.read_bytes()).hexdigest(), archive_bytes=output.stat().st_size, archive_contents_verified=True)
(output.parent/'publication-report.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
print(json.dumps(report))
