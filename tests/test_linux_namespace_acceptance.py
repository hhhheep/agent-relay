"""Acceptance isolation must reject inputs escaping its private filesystem."""
import importlib.util
import json
from pathlib import Path
import unittest
from tests.helpers import WorkspaceTempDir

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('namespace_acceptance', ROOT / 'scripts/release/run_linux_namespace.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class NamespaceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTempDir()
        self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        base = self.root / 'sandbox'
        base.mkdir()
        self.value = {'sandbox_root': str(base), 'profile_id': 'synthetic'}
        for key in ('release_root', 'python_root', 'venv_root', 'workplace_root', 'run_root', 'runtime_root', 'state_root'):
            path = base / key
            path.mkdir()
            self.value[key] = str(path)
        self.profile = {'profiles': {'synthetic': {
            'solution_agent_root': '/opt/synthetic-relay', 'remote_run_root': '/var/synthetic-runs',
            'remote_terminals': {'default': 'main', 'slots': {'main': {'cwd': '/work/synthetic'}}}}}}
        self.path = base / 'config.json'

    def write(self):
        cfg = Path(self.value['release_root']) / 'config'
        cfg.mkdir(exist_ok=True)
        (cfg / 'harness_remote_deploy_profiles.yaml').write_text(json.dumps(self.profile))
        self.path.write_text(json.dumps(self.value))

    def test_valid_fixture_maps_original_paths_without_rewriting_profile(self):
        self.write()
        original = (Path(self.value['release_root']) / 'config/harness_remote_deploy_profiles.yaml').read_bytes()
        resolved = helper.config(self.path)
        self.assertEqual(resolved['original_release_root'], '/opt/synthetic-relay')
        self.assertEqual((Path(self.value['release_root']) / 'config/harness_remote_deploy_profiles.yaml').read_bytes(), original)

    def test_external_state_root_rejected_before_mountpoint_creation(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.value['state_root'] = str(outside)
        self.write()
        with self.assertRaisesRegex(ValueError, 'state_root'):
            helper.config(self.path)
        self.assertFalse((Path(self.value['release_root']) / 'state').exists())

    def test_overlapping_historical_paths_rejected(self):
        self.profile['profiles']['synthetic']['remote_run_root'] = '/opt/synthetic-relay/state'
        self.write()
        with self.assertRaisesRegex(ValueError, 'disjoint'):
            helper.config(self.path)
        self.assertFalse((Path(self.value['release_root']) / 'state').exists())


if __name__ == '__main__':
    unittest.main()
