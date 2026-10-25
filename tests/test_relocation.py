import json
import sqlite3

from tools.relocate_project import migrate_database, rewrite_paths, text_changes


def test_path_rewrite_preserves_unrelated_names_and_uses_complete_root(tmp_path):
    old, new = tmp_path / 'flow_imagens', tmp_path / 'photoyield'
    original = {'path': str(old / 'data/image.png'), 'nested': [old.as_posix() + '/file.jpg'],
                'title': 'flow_imagens', 'url': 'https://github.com/Nodrack/flow_imagens',
                'other_folder': str(old) + '-other/file.png'}
    result = rewrite_paths(original, old, new)
    assert result['path'] == str(new / 'data/image.png')
    assert result['nested'] == [new.as_posix() + '/file.jpg']
    assert result['title'] == original['title'] and result['url'] == original['url']
    assert result['other_folder'] == original['other_folder']


def test_database_dry_run_and_apply_only_update_paths(tmp_path):
    old, new = tmp_path / 'flow_imagens', tmp_path / 'photoyield'
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE assets(id TEXT PRIMARY KEY, path TEXT, export_json TEXT, title TEXT)')
    path = str(old / 'data/image.png')
    db.execute('INSERT INTO assets VALUES (?, ?, ?, ?)', ('asset', path, json.dumps({'path': path}), 'flow_imagens'))
    expected = {'assets.path': 1, 'assets.export_json': 1}
    assert migrate_database(db, old, new) == expected
    assert db.execute('SELECT path FROM assets').fetchone()[0] == path
    assert migrate_database(db, old, new, apply=True) == expected
    row = db.execute('SELECT * FROM assets').fetchone()
    assert row[0] == 'asset' and row[1] == str(new / 'data/image.png') and row[3] == 'flow_imagens'
    assert json.loads(row[2])['path'] == row[1]
    assert migrate_database(db, old, new) == {}
    db.close()


def test_json_paths_and_activation_files_are_updated_but_backups_are_not(tmp_path):
    old, new = tmp_path / 'flow_imagens', tmp_path / 'photoyield'
    (old / 'data').mkdir(parents=True)
    (old / '.venv').mkdir()
    cfg = old / '.venv/pyvenv.cfg'
    cfg.write_text(f'command = python -m venv "{old}/.venv"', encoding='utf-8')
    reference = old / 'data/report.json'
    reference.write_text(json.dumps({'path': str(old / 'image.png')}), encoding='utf-8')
    backup = old / 'data/relocation-backup-example'
    backup.mkdir()
    (backup / 'report.json').write_text(reference.read_text(), encoding='utf-8')
    edits = dict(text_changes(old, old, new))
    assert set(edits) == {cfg, reference}
    assert str(new) in edits[cfg]
    assert json.loads(edits[reference])['path'] == str(new / 'image.png')
