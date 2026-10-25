"""One-time relocation of PhotoYield's stored paths after renaming its root folder.

Dry-run by default. Apply only from the new root, with the app stopped. Backups stay in data/.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def rewrite_paths(value, old: Path, new: Path):
    if isinstance(value, dict):
        return {key: rewrite_paths(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_paths(item, old, new) for item in value]
    if isinstance(value, str):
        normalized = value.replace('\\', '/').casefold()
        prefix = old.as_posix().rstrip('/')
        if normalized == prefix.casefold() or normalized.startswith(prefix.casefold() + '/'):
            replacement = str(new) if '\\' in value[:len(prefix)] else new.as_posix()
            return replacement + value[len(prefix):]
    return value


def rewrite_cell(value: str, old: Path, new: Path) -> str:
    if value.lstrip().startswith(('{', '[')):
        try:
            parsed = json.loads(value)
        except ValueError:
            pass
        else:
            changed = rewrite_paths(parsed, old, new)
            return json.dumps(changed, ensure_ascii=False) if changed != parsed else value
    return rewrite_paths(value, old, new)


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def migrate_database(connection, old, new, *, apply=False):
    counts = {}
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info({quote(table)})')
                   if 'TEXT' in row[2].upper() or 'CHAR' in row[2].upper()]
        if not columns:
            continue
        for row in connection.execute(f'SELECT rowid, {", ".join(map(quote, columns))} FROM {quote(table)}').fetchall():
            changes = {}
            for column, value in zip(columns, row[1:]):
                if isinstance(value, str):
                    updated = rewrite_cell(value, old, new)
                    if updated != value:
                        changes[column] = updated
                        key = f'{table}.{column}'
                        counts[key] = counts.get(key, 0) + 1
            if changes and apply:
                assignments = ', '.join(f'{quote(column)} = ?' for column in changes)
                connection.execute(f'UPDATE {quote(table)} SET {assignments} WHERE rowid = ?', [*changes.values(), row[0]])
    return counts


def text_changes(root, old, new):
    paths = [root / '.venv/pyvenv.cfg', *(root / '.venv/Scripts').glob('*activate*')]
    for folder in ['data', 'metadata', 'collections', 'candidates', 'approvals', 'reviews', 'shared', 'upscales', 'delivery', '.vscode']:
        paths.extend(path for path in (root / folder).rglob('*.json') if 'relocation-backup-' not in str(path))
    for path in paths:
        if not path.is_file():
            continue
        try:
            before = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            continue
        after = before
        if path.suffix == '.json':
            try:
                parsed = json.loads(before)
                updated = rewrite_paths(parsed, old, new)
                if updated != parsed:
                    after = json.dumps(updated, ensure_ascii=False, indent=2) + ('\n' if before.endswith('\n') else '')
            except ValueError:
                pass
        for source, target in dict([(str(old), str(new)), (old.as_posix(), new.as_posix())]).items():
            after = re.sub(re.escape(source) + r'''(?=[\\/"'\s]|$)''', lambda _: target, after, flags=re.IGNORECASE)
        if after != before:
            yield path, after


def regenerate_launchers(root):
    from importlib.metadata import distributions
    from pip._vendor.distlib.scripts import ScriptMaker
    scripts = root / '.venv/Scripts'
    maker = ScriptMaker(None, str(scripts))
    maker.executable = str(scripts / 'python.exe')
    maker.clobber = True
    maker.variants = {''}
    made = []
    for distribution in distributions():
        for entry in distribution.entry_points:
            if entry.group == 'console_scripts':
                made.extend(maker.make(f'{entry.name} = {entry.value}'))
    return len(made)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-root', type=Path, required=True)
    parser.add_argument('--new-root', type=Path, required=True)
    parser.add_argument('--hub-db', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    old, new = args.old_root.resolve(), args.new_root.resolve()
    if old.parent != new.parent or old == new or new.name != 'photoyield':
        parser.error('A relocação deve renomear a pasta para photoyield no mesmo diretório pai.')
    if args.apply and (root != new or old.exists() and any(old.iterdir())):
        parser.error('Execute --apply da pasta nova, após transferir todo o conteúdo e com o app parado.')
    source = root / 'data/flow.sqlite3'
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
        plan = migrate_database(db, old, new)
    hub_rows = []
    if args.hub_db:
        with closing(sqlite3.connect(args.hub_db.as_uri() + '?mode=ro', uri=True)) as hub:
            hub.row_factory = sqlite3.Row
            hub_rows = [dict(row) for row in hub.execute("SELECT id, project, params_json, status FROM runs WHERE project='flow_imagens'")]
            if any(row['status'] in {'queued', 'running'} for row in hub_rows):
                parser.error('Há agentes ativos: encerre-os antes de renomear o projeto.')
            if hub_rows and hub.execute("SELECT 1 FROM runs WHERE project='photoyield' LIMIT 1").fetchone():
                parser.error('O destino photoyield já contém execuções; não vou misturar projetos.')
    edits = list(text_changes(root, old, new))
    summary = {'database_cells': plan, 'text_files': [str(path.relative_to(root)) for path, _ in edits],
               'agentkit_runs': len(hub_rows), 'applied': args.apply}
    if args.apply:
        backup = root / 'data' / ('relocation-backup-' + datetime.now(UTC).strftime('%Y%m%d-%H%M%S'))
        backup.mkdir(exist_ok=False)
        with closing(sqlite3.connect(source)) as db, closing(sqlite3.connect(backup / 'flow.sqlite3')) as destination:
            db.backup(destination)
        (backup / 'agentkit-project-rows.json').write_text(json.dumps(hub_rows, ensure_ascii=False, indent=2), encoding='utf-8')
        for path, _ in edits:
            target = backup / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        with closing(sqlite3.connect(source)) as db:
            with db:
                migrate_database(db, old, new, apply=True)
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('A verificação do banco falhou; consulte o backup.')
        if args.hub_db and hub_rows:
            with closing(sqlite3.connect(args.hub_db)) as hub:
                with hub:
                    for row in hub_rows:
                        hub.execute('UPDATE runs SET project=?, params_json=? WHERE id=? AND project=?',
                            ('photoyield', rewrite_cell(row['params_json'], old, new), row['id'], 'flow_imagens'))
        for path, content in edits:
            path.write_text(content, encoding='utf-8')
        for path in (root / '.venv/Scripts').glob('*.exe'):
            if path.name.lower() not in {'python.exe', 'pythonw.exe'}:
                target = backup / path.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        summary['launchers_rebuilt'] = regenerate_launchers(root)
        summary['backup'] = str(backup)
        (backup / 'manifest.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
