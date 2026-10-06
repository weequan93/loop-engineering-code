"""Complete inspection evidence uses local bytes and no model dispatch."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from reference.core import canonical_digest
from loop_engineering.contracts import ContractError
from loop_engineering import native_review_material as material
from loop_engineering.workspace import byte_digest


class ReviewMaterialTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.authorities = self.root / 'authorities'
        self.authorities.mkdir()
        self.records = {}
        self.store = SimpleNamespace(directory=self.root,
            authorities=SimpleNamespace(directory=self.authorities),
            evidence=lambda run, ids: [deepcopy(self.records[i]) for i in ids])
        self.controller = SimpleNamespace(store=self.store, environment_digest=lambda child: 'current-env',
            _check_artifacts=lambda records: None, _origin=lambda child, digest, record: None)
        self.child = {'run_id': 'run-fixture', 'task': {'task_id': 'fixture'},
            'selected_evidence': [], 'native': {'external_origins': {}}}
        self.snapshot = {'digest': 'current-candidate'}
        self.comparison = {'verified_dependencies': []}

    def record(self, raw=b'actual output\n', *, name='stdout', candidate='current-candidate', env='current-env'):
        path = self.root / name
        path.write_bytes(raw)
        row = {'task_id': 'fixture', 'snapshot_digest': candidate, 'environment_digest': env,
            'check_id': name, 'result': 'pass', 'procedure_digest': 'original-procedure',
            'artifacts': [{'uri': path.as_uri(), 'sha256': byte_digest(raw)}]}
        digest = canonical_digest(row)
        self.records[digest] = row
        self.child['selected_evidence'].append(digest)
        return digest, row, path

    def collect(self):
        return material.collect(self.controller, self.child, self.snapshot, self.comparison)

    def manifest(self, bundle):
        return json.loads(bundle['contents']['manifest.json'])

    def test_complete_long_binary_logs_are_readable_and_bound_without_prompt_inlining(self):
        raw = b'FIRST-BYTE-WITNESS\xff' + b'x' * 12000 + b'last-output'
        digest, original, _ = self.record(raw)
        bundle = self.collect()
        folder = self.root / 'review-evidence'
        material.write(folder, bundle)
        entry = self.manifest(bundle)['entries'][0]
        self.assertEqual(entry['record'], original)
        self.assertEqual(entry['evidence_digest'], digest)
        copy = folder / entry['artifact_copies'][0]['path']
        self.assertEqual(copy.read_bytes(), raw)
        self.assertEqual(copy.stat().st_mode & 0o777, 0o444)
        self.assertEqual(bundle['descriptor']['artifact_bytes'], len(raw))
        self.assertNotIn(str(self.root), json.dumps(bundle['descriptor']))

    def test_prior_judgments_and_dependency_environments_are_preserved_not_promoted(self):
        digest, old, _ = self.record(name='history', candidate='old-candidate', env='old-env')
        self.child['selected_evidence'].clear()
        self.child['native']['external_origins'][digest] = {}
        _, dependency, _ = self.record(name='dependency', env='dependency-env')
        self.child['selected_evidence'].clear()
        self.comparison['verified_dependencies'] = [{'checks': [dependency]}]
        entries = self.manifest(self.collect())['entries']
        self.assertEqual([e['relation'] for e in entries], ['prior_judgment', 'verified_dependency'])
        self.assertTrue(all(not e['current_for_review'] for e in entries))
        self.assertEqual(entries[0]['record'], old)
        self.assertEqual(entries[1]['record'], dependency)

    def test_shared_artifact_bytes_are_deduplicated_without_losing_record_bindings(self):
        self.record(name='first')
        self.record(name='second')
        bundle = self.collect()
        self.assertEqual(bundle['descriptor']['record_count'], 2)
        self.assertEqual(bundle['descriptor']['artifact_count'], 1)

    def test_missing_corrupt_symlink_authority_and_remote_uri_artifacts_are_refused(self):
        _, row, path = self.record()
        original = path.read_bytes()
        for mode in ('missing', 'corrupt', 'symlink', 'authority', 'remote'):
            with self.subTest(mode=mode):
                if path.exists() or path.is_symlink(): path.unlink()
                path.write_bytes(original)
                row['artifacts'][0]['uri'] = path.as_uri()
                if mode == 'missing': path.unlink()
                elif mode == 'corrupt': path.write_bytes(b'changed')
                elif mode == 'symlink':
                    target = self.root / 'target'; target.write_bytes(original)
                    path.unlink(); path.symlink_to(target)
                elif mode == 'authority':
                    target = self.authorities / 'private'; target.write_bytes(original)
                    row['artifacts'][0]['uri'] = target.as_uri()
                else: row['artifacts'][0]['uri'] = 'file://untrusted' + str(path)
                with self.assertRaises((ContractError, OSError)):
                    self.collect()

    def test_file_total_record_artifact_and_manifest_bounds_fail_without_truncation(self):
        self.record(b'first', name='first')
        self.record(b'second', name='second')
        for field, limit in [('MAX_FILE_BYTES', 4), ('MAX_TOTAL_BYTES', 8),
                             ('MAX_RECORDS', 1), ('MAX_ARTIFACTS', 1), ('MAX_MANIFEST_BYTES', 1)]:
            with self.subTest(field=field), patch.object(material, field, limit):
                with self.assertRaisesRegex(ContractError, 'limit'):
                    self.collect()

    def test_mutated_missing_extra_or_redirected_copy_cannot_become_signed_material(self):
        self.record()
        bundle = self.collect()
        for mode in ('mutated', 'missing', 'extra', 'redirected'):
            with self.subTest(mode=mode):
                folder = self.root / mode
                material.write(folder, bundle)
                path = folder / 'manifest.json'
                path.chmod(0o644)
                if mode == 'mutated': path.write_bytes(b'tampered')
                elif mode == 'missing': path.unlink()
                elif mode == 'extra': (folder / 'extra').write_text('unrequested')
                else:
                    path.unlink(); path.symlink_to(self.root / 'stdout')
                with self.assertRaisesRegex(ContractError, 'modified'):
                    material.verify(folder, bundle)

    def test_project_links_are_not_followed_as_additional_evidence(self):
        self.record(b'See file:///outside/private-state/authorities/key.json\n')
        bundle = self.collect()
        self.assertEqual(bundle['descriptor']['artifact_count'], 1)
        self.assertEqual(set(bundle['contents']), {'manifest.json', 'artifacts/' + byte_digest(
            b'See file:///outside/private-state/authorities/key.json\n')[7:]})
