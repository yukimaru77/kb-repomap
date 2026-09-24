"""Validate completed paper-plus-official KBs and select portable public metadata."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from kb_api import COMPACTION_TYPES
from kb_items import load_items
from kb_source import source_address
import kb_store as store


OFFICIAL_POLICY = 'main-paper-base-plus-parallel-primary-secondary-official-memories'


def read(path):
    return json.loads(Path(path).read_text())


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def count(value, *, positive=False):
    return type(value) is int and value >= (1 if positive else 0)


def source_info(manifest):
    source = manifest.get('source_git') or {}
    commit = source.get('source_commit')
    source_hash = manifest.get('source_sha256')
    require(isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit)
            and source.get('repository_url'), '論文KBには元Gitリポジトリとコミットが必要です')
    require(isinstance(source_hash, str) and re.fullmatch(r'[0-9a-f]{64}', source_hash),
            'paper-kbの入力ハッシュが不正です')
    subdir = source.get('subdir', '.')
    require(isinstance(subdir, str) and subdir and not PurePosixPath(subdir).is_absolute()
            and '..' not in PurePosixPath(subdir).parts, '元Gitのsubdirが不正です')
    info = {'repository_url': source_address(source['repository_url']), 'source_commit': commit,
            'branch': source.get('branch'), 'subdir': subdir, 'source_sha256': source_hash}
    store.verify_source_commit(info)
    return info


def git_file(source, filename, *, optional=False):
    repo = store.cached_repo(source['repository_url'], 'sources')
    path = (PurePosixPath(source['subdir']) / filename).as_posix()
    require(not PurePosixPath(path).is_absolute() and '..' not in PurePosixPath(path).parts,
            '元Gitの資料パスが不正です')
    result = store.git(repo, 'show', f'{source["source_commit"]}:{path}', check=not optional, text=False)
    return None if result.returncode else result.stdout


def blobs(path):
    items = load_items(path)
    require(all(item.get('type') in COMPACTION_TYPES for item in items), 'KBは暗号化compaction blobのみ保存できます')
    return items


def validate_official_paper(run):
    """Read the real completed run; never modify it or synthesize result.json."""
    run = Path(run).expanduser().resolve()
    manifest, report = read(run / 'manifest.json'), read(run / 'report.json')
    require(manifest.get('policy') == OFFICIAL_POLICY and report.get('policy') == OFFICIAL_POLICY,
            '未対応の公式資料KB形式です')
    require(report.get('status') == 'complete' and report.get('errors') == [], '公式資料KBが完了していません')
    items, official = blobs(run / 'kb.json'), blobs(run / 'official-kb.json')
    nmain, nofficial, nresources = [report.get(k) for k in ('main_blobs', 'official_blobs', 'official_resources')]
    require(all(count(n, positive=True) for n in (nmain, nofficial, nresources))
            and count(report.get('total_blobs')) and count(report.get('completed_resources'))
            and report['total_blobs'] == len(items) == nmain + nofficial
            and len(official) == nofficial and report['completed_resources'] == nresources,
            '公式資料KBの完了結果とblob数が一致しません')
    for total, completed in (('primary_chunks', 'completed_primary_chunks'),
                             ('secondary_blobs', 'completed_secondary_blobs')):
        if total in report or completed in report:
            require(count(report.get(total)) and count(report.get(completed))
                    and report[total] == report[completed], '公式資料KBの圧縮処理が未完了です')
    rows = report.get('resources')
    require(isinstance(rows, list) and len(rows) == nresources
            and all(isinstance(row, dict) and isinstance(row.get('id'), str) for row in rows)
            and len({row['id'] for row in rows}) == nresources
            and all(count(row.get('official_blobs', 1), positive=True) for row in rows)
            and sum(row.get('official_blobs', 1) for row in rows) == nofficial,
            '公式資料一覧とblob数が一致しません')
    main_run = Path(manifest['main_run']).expanduser()
    if not main_run.is_absolute():
        main_run = run / main_run
    main_manifest, main_report, state = [read(main_run / name) for name in ('manifest.json', 'report.json', 'state.json')]
    require(sha((main_run / 'kb.json').read_bytes()) == manifest.get('main_kb_sha256')
            and sha((main_run / 'state.json').read_bytes()) == manifest.get('main_state_sha256'),
            '本論文KBまたはcheckpointのハッシュが変わっています')
    main = blobs(main_run / 'kb.json')
    steps = state.get('steps')
    require(main_report.get('status') == 'complete' and type(main_report.get('final_blobs')) is int
            and main_report['final_blobs'] == nmain == len(main)
            and isinstance(steps, list) and all(isinstance(step, dict) for step in steps)
            and [step.get('blob') for step in steps] == main,
            '本論文KBが完了済みcheckpointと一致しません')
    require(items == main + official, '最終KBが本論文prefixと公式資料blobの順序を保持していません')
    main_source, official_source = source_info(main_manifest), source_info(manifest)
    require(sha(git_file(main_source, 'paper.md')) == main_source['source_sha256'],
            '本論文の元Git commitとpaper.mdハッシュが一致しません')
    index_bytes = git_file(official_source, 'index.json')
    index = json.loads(index_bytes)
    require(sha(index_bytes) == manifest.get('index_sha256')
            and sha(json.dumps(index, sort_keys=True, ensure_ascii=False).encode()) == manifest['source_sha256'],
            '公式資料の元Git commitとindexハッシュが一致しません')
    indexed = index.get('resources')
    require(isinstance(indexed, list) and [row['id'] for row in indexed] == [row['id'] for row in rows],
            '公式資料KB一覧と元Git indexの収録順序が一致しません')
    for row in indexed:
        for filename, key in (('paper.md', 'paper_sha256'), ('source.json', 'source_sha256')):
            require(sha(git_file(official_source, str(PurePosixPath(row['directory']) / filename))) == row.get(key),
                    '公式資料の元Git payloadハッシュが一致しません: ' + row['id'])
    bibliography = git_file(main_source, 'references.json', optional=True)
    references = None
    if bibliography is not None:
        entries = json.loads(bibliography).get('references')
        require(isinstance(entries, list) and all(isinstance(r, dict) and 'id' in r for r in entries)
                and len({r['id'] for r in entries}) == len(entries), '元Gitの引用文献一覧が不正です')
        references = len(entries)
    return {'manifest': manifest, 'report': report, 'snapshot': run / 'kb.json',
            'main_source': main_source, 'official_source': official_source, 'references': references}


def public_source(source, override, flag):
    published = dict(source)
    if override:
        published['repository_url'] = source_address(override)
        # The archive may preserve this commit on a different branch. Do not
        # reinterpret the original local `main` as the new remote's main branch.
        published['branch'] = None
    require(not Path(published['repository_url']).is_absolute(),
            f'公開情報にローカル絶対パスを保存しません。同じcommitを含むGit URLを{flag}で指定してください')
    # A relocated Git URL must actually contain the original commit, not merely
    # be a convenient upstream repository with a similar name.
    store.verify_source_commit(published)
    return published


def official_paper_info(run, *, source_repository_url=None, main_source_repository_url=None):
    verified = validate_official_paper(run)
    source = public_source(verified['official_source'], source_repository_url, '--source-repository-url')
    main_source = public_source(verified['main_source'], main_source_repository_url, '--main-source-repository-url')
    report, manifest = verified['report'], verified['manifest']
    info = {'source_kind': 'paper', 'paper_layout': 'main-plus-official-resources', **source,
            'main_source_git': main_source, 'reference_blobs': 0, 'main_blobs': report['main_blobs'],
            'official_resources': report['official_resources'], 'official_blobs': report['official_blobs'],
            'total_blobs': report['total_blobs'], 'main_kb_sha256': manifest['main_kb_sha256'],
            'build': {key: manifest[key] for key in ('model', 'effort', 'budget_tokens', 'include_images') if key in manifest}}
    if verified['references'] is not None:
        info['references'] = verified['references']
    return info
