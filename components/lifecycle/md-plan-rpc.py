
# Independent extraction: resolve the sole project Python source, never a research checkout.
import sys as _extraction_sys
from pathlib import Path as _ExtractionPath
_extraction_sys.path.insert(0, str(_ExtractionPath(__file__).resolve().parents[2]))

"""Build the explicit production worker instruction plan, without remote I/O."""
import argparse
import json
from pathlib import Path
from solution_agent.harness.task_md_sync import plan_sync

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--allowlist', type=Path, required=True)
    parser.add_argument('--remote-python', required=True)
    parser.add_argument('--remote-workplace', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.allowlist.read_text(encoding='utf-8'))
    if config.get('schema') != 'harness-worker-md-allowlist/v1':
        raise ValueError('allowlist schema required')
    plan = plan_sync(args.source_root, config['files'], remote_python=args.remote_python, remote_workplace=args.remote_workplace)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'plan_id': plan['plan_id'], 'version': plan['version'], 'files': len(plan['manifest']),
                      'source_bytes': sum((args.source_root/name).stat().st_size for name in config['files']),
                      'encoded_command_bytes': len(plan['command'].encode()), 'remote_executed': False}))
