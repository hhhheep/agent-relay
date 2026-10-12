"""Check the exact Git publication candidate; never print matched secret values."""
from pathlib import Path
import ipaddress, json, re, subprocess, sys
ROOT = Path(__file__).resolve().parents[2]
SECRET = re.compile(r'(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})')
WINDOWS_USER = re.compile(r'[A-Za-z]:[/\\]+Users[/\\]+([^/\\\s\"\'`]+)')
HOME_USER = re.compile(r'/home' + r'/([^/\s\"\'`]+)')
IP = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
EXAMPLE_USERS = {'example-user', 'agent-user', 'agent', 'demo', 'user', 'test', 'tester', 'Public', 'Default'}
FORBIDDEN_PARTS = {'.git', '.venv', '.deps', 'node_modules', '.local-extraction', '.verification', '__pycache__', 'state', 'test-runs', '.solution', '.codex-orchestrator'}

def publication_files():
    result = subprocess.run(['git', '--no-optional-locks', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT, capture_output=True, check=True)
    return sorted(set(result.stdout.decode('utf-8').split('\0')) - {''})

def inspect(names):
    findings = []
    for name in names:
        relative = Path(name)
        p = ROOT / relative
        if relative.is_absolute() or '..' in relative.parts or ROOT not in p.resolve().parents:
            findings.append({'file': name, 'category': 'path_boundary'}); continue
        if FORBIDDEN_PARTS.intersection(relative.parts) or p.is_symlink() or any(getattr(a, 'is_junction', lambda: False)() for a in p.parents if a != ROOT):
            findings.append({'file': name, 'category': 'private_or_dependency_file'}); continue
        if name in {'registry.json', 'registry.json.lock'} or relative.parts[0] in {'tasks', 'dispatch', 'worktrees', 'results', 'reports', 'executions', 'outputs'} or p.name == '.env' or p.name.startswith('.env.') and p.name != '.env.example' or '.local.' in p.name or p.suffix in {'.db', '.sqlite', '.exe', '.zip', '.pyc'}:
            findings.append({'file': name, 'category': 'local_file'}); continue
        try: text = p.read_text(encoding='utf-8-sig')
        except UnicodeError:
            findings.append({'file': name, 'category': 'unreviewed_binary'}); continue
        if SECRET.search(text): findings.append({'file': name, 'category': 'credential_literal'})
        for label, pattern in [('personal_windows_path', WINDOWS_USER), ('personal_home_path', HOME_USER)]:
            if any(m.group(1) not in EXAMPLE_USERS and not m.group(1).startswith(('<', '${', '$')) for m in pattern.finditer(text)):
                findings.append({'file': name, 'category': label})
        for m in IP.finditer(text):
            try: address = ipaddress.ip_address(m.group())
            except ValueError: continue
            if address.is_loopback or address.is_private or m.group() == '255.255.255.255': continue
            findings.append({'file': name, 'category': 'non_example_public_ip'}); break
    return {'files_checked': len(names), 'findings': findings, 'passed': not findings}

if __name__ == '__main__':
    report = inspect(publication_files())
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(not report['passed'])


