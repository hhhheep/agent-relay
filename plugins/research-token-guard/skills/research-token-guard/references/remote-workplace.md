# Remote workplace deployment

Use this reference only for an SSH workplace.

1. Copy the plugin source to a versioned or otherwise recoverable directory on the workplace. Do not overwrite an unrelated tool directory.
2. Confirm Python 3.9 or newer with `python3 --version`.
3. Run the complete local test suite before copying, then run `doctor` and the smoke tests on the workplace.
4. Keep artifacts inside the selected project root, normally under `.token-guard/runs/`.
5. Add only the skill/plugin registration required by that Codex installation. Do not enable a global shell hook.
6. Start a new Codex task after installation so the skill inventory is rebuilt.

Validation sequence:

```text
python3 <skill-dir>/scripts/token_guard.py doctor
python3 <skill-dir>/scripts/token_guard.py run --root <project> --cwd <project> -- python3 -c "print('small-output')"
python3 <skill-dir>/scripts/token_guard.py verify --root <project> --receipt <receipt-path>
```

For a real research validation, replay saved logs or deterministic analysis outputs first. Do not spend GPU/API resources solely to prove output compression.
