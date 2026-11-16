"""Import Wikipedia references from a revision-pinned LessWrong snapshot.

Only article URLs and source metadata are retained; forum bodies are not republished.
"""
import hashlib
import json
import shutil
import sqlite3
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from .sources import request_json
from .store import get_meta, set_meta, timestamp, wiki_url, merge_metadata

DATASET = 'x65617379/lesswrong_260509'


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.urls.append(dict(attrs).get('href') or '')


def references(item):
    parser = Links()
    parser.feed(item.get('htmlBody') or '')
    result = {}
    for url in [item.get('url') or '', *parser.urls]:
        if url.startswith('//'):
            url = 'https:' + url
        article = wiki_url(url)
        if article:
            result[article['id']] = article
    return list(result.values())


def setup(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS source_items(
            source TEXT NOT NULL,id TEXT NOT NULL,author_id TEXT,author TEXT,created_at INTEGER,
            kind TEXT,url TEXT,story_id TEXT,story_title TEXT,story_url TEXT,points INTEGER,revision TEXT,
            PRIMARY KEY(source,id));
        CREATE INDEX IF NOT EXISTS source_items_story ON source_items(source,story_id);
        CREATE TABLE IF NOT EXISTS source_mentions(
            source TEXT NOT NULL,item_id TEXT NOT NULL,article_id TEXT NOT NULL REFERENCES articles(id),
            PRIMARY KEY(source,item_id,article_id),
            FOREIGN KEY(source,item_id) REFERENCES source_items(source,id) ON DELETE CASCADE);
        CREATE INDEX IF NOT EXISTS source_mentions_article ON source_mentions(article_id);
    ''')


def ingest_rows(db, rows, revision='local'):
    count = 0
    for row in rows:
        post = row['post']
        post_id = str(post['_id'])
        # Re-importing a thread replaces its references, including removed links.
        db.execute("DELETE FROM source_items WHERE source='lesswrong' AND story_id=?", (post_id,))
        story_url = f'https://www.lesswrong.com/posts/{quote(post_id, safe="")}'
        for kind, item in [('story', post), *[('comment', c) for c in row.get('comments') or []]]:
            if item.get('deleted') or item.get('deletedPublic') or item.get('retracted'):
                continue
            articles = references(item)
            if not articles:
                continue
            item_id = str(item['_id'])
            author = item.get('user') or {}
            url = story_url + ('?commentId=' + quote(item_id, safe='') if kind == 'comment' else '')
            db.execute('INSERT OR REPLACE INTO source_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('lesswrong', item_id, author.get('_id'), author.get('displayName') or author.get('username') or '[deleted]',
                        timestamp(item.get('postedAt')), kind, url, post_id, post['title'], story_url,
                        item.get('baseScore') or 0, revision))
            for article in articles:
                alias = db.execute('SELECT canonical FROM aliases WHERE alias=?', (article['id'],)).fetchone()
                if alias:
                    canonical = db.execute('SELECT id,lang,title,url FROM articles WHERE id=?', (alias[0],)).fetchone()
                    if canonical:
                        article = dict(canonical)
                db.execute('INSERT OR IGNORE INTO articles(id,lang,title,url) VALUES (?,?,?,?)',
                           tuple(article[key] for key in ('id', 'lang', 'title', 'url')))
                db.execute('INSERT OR IGNORE INTO source_mentions VALUES (?,?,?)', ('lesswrong', item_id, article['id']))
                count += 1
    return count


def canonicalize(db, topic_cache='.data/topic-predictions.sqlite', popularity_cache='.data/wikipedia-popularity.sqlite'):
    """Resolve cached redirects and discard missing LW targets without network calls."""
    if not Path(topic_cache).is_file():
        return {'redirects': 0, 'missing': 0}
    cache = sqlite3.connect(topic_cache)
    popularity = sqlite3.connect(popularity_cache) if Path(popularity_cache).is_file() else None
    result = {'redirects': 0, 'missing': 0}
    try:
        rows = db.execute('SELECT DISTINCT a.* FROM articles a JOIN source_mentions m ON m.article_id=a.id').fetchall()
        with db, cache:
            for row in rows:
                prediction = cache.execute('SELECT canonical,status,scores,checked_at FROM predictions WHERE lang=? AND title=?',
                                           (row['lang'], row['title'])).fetchone()
                if not prediction:
                    continue
                title, status, scores, at = prediction
                if status == 'missing':
                    db.execute("DELETE FROM source_mentions WHERE source='lesswrong' AND article_id=?", (row['id'],))
                    result['missing'] += 1
                    continue
                canonical = wiki_url(f'https://{row["lang"]}.wikipedia.org/wiki/{quote(title.replace(" ", "_"), safe="")}')
                if not canonical:
                    db.execute("DELETE FROM source_mentions WHERE source='lesswrong' AND article_id=?", (row['id'],))
                    result['missing'] += 1
                    continue
                if row['id'] == canonical['id'] or not db.execute('SELECT 1 FROM articles WHERE id=?', (row['id'],)).fetchone():
                    continue
                existing = db.execute('SELECT metadata,enriched_at FROM articles WHERE id=?', (canonical['id'],)).fetchone()
                metadata = {**json.loads(row['metadata']), **(json.loads(existing['metadata']) if existing else {}), **canonical}
                enriched_at = max(row['enriched_at'], existing['enriched_at'] if existing else 0)
                merge_metadata(db, row['id'], metadata)
                db.execute('UPDATE articles SET enriched_at=? WHERE id=?', (enriched_at, canonical['id']))
                cache.execute('INSERT OR IGNORE INTO predictions VALUES (?,?,?,?,?,?)', (row['lang'], title, title, status, scores, at))
                if popularity:
                    value = popularity.execute('SELECT status,score,checked_at FROM popularity WHERE lang=? AND title=?',
                                               (row['lang'], row['title'])).fetchone()
                    if value:
                        popularity.execute('INSERT OR IGNORE INTO popularity VALUES (?,?,?,?,?,?)', (row['lang'], title, title, *value))
                result['redirects'] += 1
        if popularity:
            popularity.commit()
    finally:
        cache.close()
        if popularity:
            popularity.close()
    return result


def import_snapshot(db, dataset=DATASET, revision=None, max_posts=None, file=None, log=print):
    setup(db)
    if file:
        files = [{'path': str(Path(file).resolve())}]
        revision = 'local-' + hashlib.sha256(Path(file).read_bytes()).hexdigest()[:16]
    else:
        info = request_json(f'https://huggingface.co/api/datasets/{dataset}' + (f'/revision/{revision}' if revision else ''))
        revision = info['sha']
        files = request_json(f'https://huggingface.co/api/datasets/{dataset}/tree/{revision}?recursive=true&limit=1000')
        files = sorted((entry for entry in files if entry['path'].endswith('.parquet')), key=lambda entry: entry['path'])
        if not files:
            raise ValueError('No LessWrong Parquet shards found.')
    checkpoint = get_meta(db, 'lesswrong_snapshot', {})
    if checkpoint.get('revision') != revision or checkpoint.get('dataset') != dataset:
        checkpoint = {'dataset': dataset, 'revision': revision, 'scope': 'file' if file else 'snapshot', 'files': {}, 'posts': 0, 'complete': False}
    imported = 0
    cache = Path('.data/lesswrong-downloads')
    cache.mkdir(parents=True, exist_ok=True)
    for entry in files:
        name = entry['path']
        progress = checkpoint['files'].get(name, {'rows': 0, 'complete': False})
        if progress['complete']:
            continue
        local = Path(file) if file else cache / (revision + '-' + Path(name).name)
        if not file and (not local.exists() or local.stat().st_size != entry['size']):
            log(f"Downloading LessWrong shard {name} ({entry['size'] / 1e6:.1f} MB)…", flush=True)
            url = f'https://huggingface.co/datasets/{dataset}/resolve/{revision}/{quote(name, safe="/")}'
            request = Request(url, headers={'User-Agent': 'xWiki/0.2 (Wikipedia link discovery)'})
            pending = local.with_suffix('.part')
            with urlopen(request, timeout=90) as response, pending.open('wb') as target:
                shutil.copyfileobj(response, target, length=1024 * 1024)
            if pending.stat().st_size != entry['size']:
                raise ValueError('Incomplete LessWrong download; rerun to retry.')
            pending.replace(local)
        import duckdb
        connection = duckdb.connect()
        try:
            connection.execute("SET memory_limit='512MB'")
            connection.execute('SET threads=2')
            cursor = connection.execute('SELECT post,comments FROM read_parquet(?) LIMIT 1000000000 OFFSET ?', [str(local), progress['rows']])
            while True:
                remaining = 64 if max_posts is None else min(64, max_posts - imported)
                if remaining <= 0:
                    break
                records = cursor.fetchmany(remaining)
                if not records:
                    progress['complete'] = True
                    break
                with db:
                    ingest_rows(db, [{'post': post, 'comments': comments} for post, comments in records], revision)
                    progress['rows'] += len(records)
                    checkpoint['posts'] += len(records)
                    checkpoint['files'][name] = progress
                    set_meta(db, 'lesswrong_snapshot', checkpoint)
                imported += len(records)
                if checkpoint['posts'] % 1024 == 0:
                    log(f"LessWrong: {checkpoint['posts']:,} posts processed; checkpoints saved.", flush=True)
        finally:
            connection.close()
        checkpoint['files'][name] = progress
        with db:
            set_meta(db, 'lesswrong_snapshot', checkpoint)
        if max_posts is not None and imported >= max_posts:
            break
    checkpoint['complete'] = all(checkpoint['files'].get(entry['path'], {}).get('complete') for entry in files)
    checkpoint['at'] = int(time.time())
    with db:
        if checkpoint['complete'] and not file:
            db.execute("DELETE FROM source_items WHERE source='lesswrong' AND revision!=?", (revision,))
        set_meta(db, 'lesswrong_snapshot', checkpoint)
    log(f"LessWrong: {checkpoint['posts']:,} posts; {'complete' if checkpoint['complete'] else 'partial, rerun to resume'}.", flush=True)
    log(f'LessWrong cached target cleanup: {canonicalize(db)}', flush=True)
    return checkpoint
