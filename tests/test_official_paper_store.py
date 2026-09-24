import contextlib
import io
import json
import subprocess
import unittest
from unittest import mock

import test_kb_cli
import kb_cli
import kb_paper
import kb_store
from kb_items import load_items


class OfficialPaperStoreTest(unittest.TestCase):
    setUp = test_kb_cli.GitStoreTest.setUp
    repo = test_kb_cli.GitStoreTest.repo
    commit = test_kb_cli.GitStoreTest.commit
    PUBLIC_URL = 'https://example.test/author/paper-kb.git'

    def fixture(self):
        (self.source / 'paper.md').write_text('# Original main paper\nExact source text.\n')
        (self.source / 'references.json').write_text(json.dumps({'references': [{'id': i} for i in range(1, 5)]}))
        main_commit = self.commit(self.source)
        self.official_source = self.repo('official-source')
        indexed = []
        for identifier in ('implementation', 'release-assets'):
            directory = self.official_source / identifier
            directory.mkdir()
            (directory / 'paper.md').write_text('# ' + identifier + '\nCaptured source text.\n')
            (directory / 'source.json').write_text(json.dumps({'id': identifier, 'private_path': '/private/acquisition'}))
            indexed.append({'id': identifier, 'directory': identifier,
                            'paper_sha256': kb_paper.sha((directory / 'paper.md').read_bytes()),
                            'source_sha256': kb_paper.sha((directory / 'source.json').read_bytes())})
        index = {'resources': indexed}
        (self.official_source / 'index.json').write_text(json.dumps(index))
        official_commit = self.commit(self.official_source)
        self.main = self.root / 'main-run'
        self.main.mkdir()
        (self.main / 'kb.json').write_text(json.dumps(self.blobs[:2]))
        (self.main / 'state.json').write_text(json.dumps({'steps': [{'blob': b, 'account': 'private-account'} for b in self.blobs[:2]]}))
        (self.main / 'report.json').write_text(json.dumps({'status': 'complete', 'final_blobs': 2}))
        (self.main / 'manifest.json').write_text(json.dumps({
            'source_sha256': kb_paper.sha((self.source / 'paper.md').read_bytes()),
            'source_git': {'repository_url': str(self.source), 'source_commit': main_commit, 'branch': 'main'}}))
        self.run = self.root / 'official-run'
        self.run.mkdir()
        (self.run / 'kb.json').write_text(json.dumps(self.blobs[:5]))
        (self.run / 'official-kb.json').write_text(json.dumps(self.blobs[2:5]))
        self.manifest = {'policy': kb_paper.OFFICIAL_POLICY,
            'source_git': {'repository_url': str(self.official_source), 'source_commit': official_commit, 'branch': 'main'},
            'source_sha256': kb_paper.sha(json.dumps(index, sort_keys=True, ensure_ascii=False).encode()),
            'index_sha256': kb_paper.sha((self.official_source / 'index.json').read_bytes()),
            'main_run': str(self.main), 'main_kb_sha256': kb_paper.sha((self.main / 'kb.json').read_bytes()),
            'main_state_sha256': kb_paper.sha((self.main / 'state.json').read_bytes()),
            'model': 'gpt-6-astra', 'effort': 'low', 'budget_tokens': 150000, 'include_images': False,
            'accounts': ['private-account'], 'origin': 'http://private-pool:1234', 'instructions': 'private prompt'}
        (self.run / 'manifest.json').write_text(json.dumps(self.manifest))
        self.report = {'status': 'complete', 'policy': kb_paper.OFFICIAL_POLICY, 'errors': [],
            'main_blobs': 2, 'official_resources': 2, 'completed_resources': 2, 'official_blobs': 3, 'total_blobs': 5,
            'primary_chunks': 7, 'completed_primary_chunks': 7, 'secondary_blobs': 3, 'completed_secondary_blobs': 3,
            'resources': [{'id': 'implementation', 'official_blobs': 2}, {'id': 'release-assets', 'official_blobs': 1}]}
        self.save_report()
        kb_store.write_config(self.config)
        self.real_verify = kb_store.verify_source_commit
        self.source_by_commit = {main_commit: self.source, official_commit: self.official_source}

    def save_report(self):
        (self.run / 'report.json').write_text(json.dumps(self.report))

    def verify_public_locally(self, source):
        if source['repository_url'] == self.PUBLIC_URL:
            self.assertIsNone(source['branch'])
            source = {**source, 'repository_url': str(self.source_by_commit[source['source_commit']])}
        return self.real_verify(source)

    def command(self):
        return ['publish-paper', 'paper', '--run', str(self.run), '--store', 'second',
                '--source-repository-url', self.PUBLIC_URL, '--main-source-repository-url', self.PUBLIC_URL]

    def test_final_report_publish_preserves_blobs_git_truth_and_private_metadata(self):
        self.fixture()
        original = {p: p.read_bytes() for folder in (self.run, self.main) for p in folder.iterdir()}
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(kb_store, 'verify_source_commit', side_effect=self.verify_public_locally):
            kb_cli.main(self.command())
        loaded = kb_store.find_kb(self.config, 'paper', 'second')
        info = loaded['info']
        self.assertEqual(load_items(loaded['jsonl']), self.blobs[:5])
        self.assertEqual((info['references'], info['reference_blobs'], info['main_blobs'],
                          info['official_resources'], info['official_blobs']), (4, 0, 2, 2, 3))
        self.assertEqual(info['source_commit'], self.manifest['source_git']['source_commit'])
        self.assertEqual(info['main_source_git']['source_commit'], json.loads((self.main / 'manifest.json').read_text())['source_git']['source_commit'])
        self.assertIsNone(info['branch'])
        self.assertIsNone(info['main_source_git']['branch'])
        for private in ('private-account', 'private-pool', 'private prompt', str(self.root), 'main_run', 'accounts'):
            self.assertNotIn(private, json.dumps(info))
        self.assertEqual(original, {p: p.read_bytes() for p in original})
        self.assertFalse((self.run / 'result.json').exists())
        files = kb_store.git(kb_store.cached_repo(self.store2['url'], 'stores'), 'ls-tree', '-r', '--name-only',
                             loaded['store_revision'], '--', 'paper').stdout.splitlines()
        self.assertEqual(files, ['paper/info.json', 'paper/latest.json'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(kb_cli.kb_codex, 'start_session', return_value='id'):
            kb_cli.main(['codex', 'paper', '--store', 'second', '--session-only'])
        self.assertIn('本論文blob 2個 / 公式資料 2件 / 公式blob 3個', output.getvalue())

    def test_local_source_urls_are_not_leaked_to_public_info(self):
        self.fixture()
        with mock.patch.object(kb_store, 'publish') as publish, self.assertRaisesRegex(ValueError, '絶対パス'):
            kb_cli.main(['publish-paper', 'paper', '--run', str(self.run), '--store', 'second'])
        publish.assert_not_called()

    def test_official_publish_includes_run_developer_and_preserves_it_when_missing(self):
        self.fixture()
        developer = ".\n├── official/ (Official code)\n└── translation/ (Translated paper)\n"
        (self.run / 'dev.txt').write_text(developer, encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(kb_store, 'verify_source_commit', side_effect=self.verify_public_locally):
            kb_cli.main(self.command())
        loaded = kb_store.find_kb(self.config, 'paper', 'second')
        self.assertEqual(loaded['developer_text'], developer)
        self.assertEqual(load_items(loaded['jsonl']), self.blobs[:5])
        (self.run / 'dev.txt').unlink()
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(kb_store, 'verify_source_commit', side_effect=self.verify_public_locally):
            kb_cli.main(self.command())
        repeated = kb_store.find_kb(self.config, 'paper', 'second')
        self.assertEqual(repeated['developer_text'], developer)
        self.assertEqual(repeated['store_revision'], loaded['store_revision'])

    def test_incomplete_counts_and_changed_main_prefix_rejected(self):
        self.fixture()
        for key, value in (('main_blobs', True), ('total_blobs', 6), ('completed_resources', 1),
                           ('completed_primary_chunks', 6)):
            original = self.report[key]
            self.report[key] = value
            self.save_report()
            with self.subTest(key=key), mock.patch.object(kb_store, 'publish') as publish, self.assertRaises(ValueError):
                kb_cli.main(self.command())
            publish.assert_not_called()
            self.report[key] = original
        self.save_report()
        (self.run / 'kb.json').write_text(json.dumps([self.blobs[1], self.blobs[0], *self.blobs[2:5]]))
        with self.assertRaisesRegex(ValueError, 'prefix'):
            kb_paper.validate_official_paper(self.run)

    def test_git_source_bytes_and_main_checkpoint_must_match(self):
        self.fixture()
        original = (self.main / 'state.json').read_bytes()
        (self.main / 'state.json').write_bytes(original + b' ')
        with self.assertRaisesRegex(ValueError, 'checkpoint'):
            kb_paper.validate_official_paper(self.run)
        (self.main / 'state.json').write_bytes(original)
        main_manifest = json.loads((self.main / 'manifest.json').read_text())
        main_manifest['source_sha256'] = '0' * 64
        (self.main / 'manifest.json').write_text(json.dumps(main_manifest))
        with self.assertRaisesRegex(ValueError, '元Git commitとpaper.md'):
            kb_paper.validate_official_paper(self.run)

    def test_relocated_git_must_contain_the_exact_original_commit(self):
        self.fixture()
        unrelated = self.repo('unrelated-archive')
        def verify(source):
            if source['repository_url'] == self.PUBLIC_URL:
                source = {**source, 'repository_url': str(unrelated)}
            return self.real_verify(source)
        with mock.patch.object(kb_store, 'verify_source_commit', side_effect=verify), \
                mock.patch.object(kb_store, 'publish') as publish, self.assertRaises(subprocess.CalledProcessError):
            kb_cli.main(self.command())
        publish.assert_not_called()
