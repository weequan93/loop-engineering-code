"""Reinitialization archives controls, preserves code/history and rolls back."""

from copy import deepcopy
import json
from unittest.mock import patch
import unittest

from loop_engineering.contracts import ContractError
from loop_engineering.setup import reinitialize
from tests import test_team as team_fixture


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = team_fixture.TeamTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.store = self.fixture.root, self.fixture.store
        self.spec = self.fixture.base / "new-requirements.md"
        self.spec.write_text("# New requirements\nAdd a new compatible behavior and verify it.\n")

    def test_fresh_settings_archive_exact_controls_and_preserve_source_and_history(self):
        self.fixture.start()
        team_id = self.fixture.team_id
        self.fixture.team.stop(team_id, cancel=True)
        old = {path.relative_to(self.root/'.loop').as_posix():path.read_bytes()
               for path in (self.root/'.loop').rglob('*') if path.is_file()}
        source = (self.root/'slug.py').read_bytes()
        result = reinitialize(self.root, self.spec, self.store)
        from pathlib import Path
        backup = Path(result['backup_directory'])
        saved = {path.relative_to(backup/'.loop').as_posix():path.read_bytes()
                 for path in (backup/'.loop').rglob('*') if path.is_file()}
        self.assertEqual(saved, old)
        self.assertEqual((self.root/'slug.py').read_bytes(), source)
        self.assertEqual((self.root/'.loop/spec.md').read_text(), self.spec.read_text())
        self.assertEqual(json.loads((self.root/'.loop/workflow-tasks.json').read_text())['tasks'], [])
        self.assertFalse(result['automatic_dispatch'])
        self.assertEqual(self.fixture.team.teams.audit(team_id)['status'],'CANCELLED')
        self.assertEqual(json.loads((backup/'setup.json').read_text())['status'],'COMPLETE')

    def test_running_or_paused_team_refuses_without_changing_settings(self):
        self.fixture.start()
        original = (self.root/'.loop/spec.md').read_bytes()
        with self.assertRaisesRegex(ContractError,'Cancel unfinished'):
            reinitialize(self.root,self.spec,self.store)
        self.fixture.team.stop(self.fixture.team_id,cancel=False)
        with self.assertRaisesRegex(ContractError,'Cancel unfinished'):
            reinitialize(self.root,self.spec,self.store)
        self.assertEqual((self.root/'.loop/spec.md').read_bytes(),original)

    def test_failed_install_restores_exact_prior_controls(self):
        old = (self.root/'.loop/spec.md').read_bytes()
        def partial(*args,**kwargs):
            (self.root/'.loop').mkdir()
            (self.root/'.loop/new-settings.json').write_text('{}')
            raise OSError('Injected installation failure')
        with patch('loop_engineering.setup.initialize',side_effect=partial):
            with self.assertRaisesRegex(OSError,'Injected'):
                reinitialize(self.root,self.spec,self.store)
        self.assertEqual((self.root/'.loop/spec.md').read_bytes(),old)
        self.assertFalse((self.root/'.loop/new-settings.json').exists())
        backup = next((self.store.directory/'setup-backups').glob('setup-*'))
        self.assertTrue((backup/'incomplete-controls/new-settings.json').exists())
        self.assertEqual(json.loads((backup/'setup.json').read_text())['status'],'ROLLED_BACK')

    def test_spec_inside_old_controls_survives_archiving(self):
        spec = self.root/'.loop/spec.md'
        original = spec.read_bytes()
        reinitialize(self.root,spec,self.store)
        self.assertEqual(spec.read_bytes(),original)

    def test_cancel_signal_does_not_erase_an_unresolved_owned_operation(self):
        from loop_engineering.team_automation import TeamAutomation
        team_id = TeamAutomation(self.fixture.team, None).start(self.root)['team_id']
        self.fixture.team.stop(team_id,cancel=True)
        data = self.fixture.team.teams.get(team_id)
        data['automation']['operations']['operation-fixture']={'status':'ADMITTED','pid':None}
        self.fixture.team.teams.save(data,'offline.unresolved_operation_fixture')
        original=(self.root/'.loop/spec.md').read_bytes()
        with self.assertRaisesRegex(ContractError,'reconcile owned'):
            reinitialize(self.root,self.spec,self.store)
        self.assertEqual((self.root/'.loop/spec.md').read_bytes(),original)

    def test_empty_spec_and_symlink_control_refuse_before_archive(self):
        self.spec.write_text(' ')
        with self.assertRaisesRegex(ContractError,'nonempty'):
            reinitialize(self.root,self.spec,self.store)
        self.assertFalse((self.store.directory/'setup-backups').exists())
        other = self.fixture.base/'other'
        other.mkdir()
        import os
        os.replace(self.root/'.loop',other/'.loop')
        (self.root/'.loop').symlink_to(other/'.loop',target_is_directory=True)
        self.spec.write_text('Actual requirements')
        with self.assertRaises(ContractError):
            reinitialize(self.root,self.spec,self.store)
        self.assertTrue((other/'.loop/spec.md').is_file())

    def test_cancelled_team_cannot_archive_unknown_setup_in_its_private_child(self):
        from loop_engineering.team_automation import TeamAutomation
        team = self.fixture.team
        team_id = TeamAutomation(team, None).start(self.root)['team_id']
        team.request(team_id, 'implement', managed=True)
        run_id = team.teams.get(team_id)['records']['implement']['child_run_id']
        child = self.store.get(run_id)
        self.assertNotEqual(child['workspace'], str(self.root))
        team.stop(team_id, cancel=True)
        before = (self.root/'.loop/spec.md').read_bytes()
        for status in ('RUNNING','FAILED','CANCELLED'):
            with self.store.writer(run_id) as child:
                child['provisioning'] = {'status':status}
                self.store.save(child,'offline.unknown_dependency_effect')
            with self.subTest(status=status), self.assertRaisesRegex(ContractError,'Reconcile pending'):
                reinitialize(self.root,self.spec,self.store)
            self.assertEqual((self.root/'.loop/spec.md').read_bytes(),before)
