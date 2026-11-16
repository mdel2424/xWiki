import json
import tempfile
import unittest
from pathlib import Path

from pipeline.lesswrong import ingest_rows, references, canonicalize
from pipeline.communities import equivalent_linkers
from pipeline.static_export import export_static
from pipeline.store import connect, ingest, merge_metadata, refresh_stats, wiki_url


def item(identity, author='a', title='Earth', **extra):
    return {'_id': identity, 'title': 'A source discussion', 'postedAt': '2026-05-01T12:00:00Z',
            'user': {'_id': author, 'displayName': 'Same display name'},
            'htmlBody': f'<a href="https://en.wikipedia.org/wiki/{title}">article</a>', **extra}


class LessWrongTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = connect(str(self.root / 'fixture.sqlite'))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_links_only_skip_namespaces_and_keep_apostrophes_and_parentheses(self):
        links = references({'htmlBody': '<a href="//en.m.wikipedia.org/wiki/Don%27t_Panic_(song)#Section">song</a>'
            '<a href="https://en.wikipedia.org/wiki/Category:Music">category</a>'
            '<a href="https://en.wikipedia.org/wiki/Main_Page">main</a>'
            '<a href="https://en.wikipedia.org/wiki/Don%27t_Panic_(song)">duplicate</a>'})
        self.assertEqual([link['id'] for link in links], ["en:Don't Panic (song)"])

    def test_export_merges_shared_articles_counts_accounts_and_preserves_source_links(self):
        records = [{'post': item('post1', baseScore=100000), 'comments': [item('c1'), item('c2', 'b'),
                    item('deleted', 'c', deleted=True), item('retracted', 'd', retracted=True)]},
                   {'post': item('post2', title='Nashville_Number_System'), 'comments': []}]
        with self.db:
            ingest(self.db, [{'id': 1, 'type': 'story', 'by': 'Same display name', 'url': 'https://en.wikipedia.org/wiki/Earth'}])
            ingest_rows(self.db, records)
            ingest_rows(self.db, records)  # Resuming must not multiply mentions.
            refresh_stats(self.db)
        manifest = export_static(self.db, self.root / 'catalog', context_size=2)
        root = self.root / 'catalog' / manifest['generation']
        cards = json.loads((root / 'articles/0.json').read_text())
        self.assertEqual(len(cards), 2)
        self.assertEqual(manifest['hnArticleCount'], 1)
        self.assertEqual(manifest['mentionCount'], 5)
        self.assertEqual(cards[0]['metrics']['distinctLinkers'], 3)
        self.assertEqual(cards[0]['sourceMetrics']['lesswrong']['distinctLinkers'], 2)
        self.assertEqual(cards[0]['sourceMetrics']['lesswrong']['mentionCount'], 3)
        self.assertNotIn('hn', cards[1]['sourceMetrics'])
        weights = json.loads((root / manifest['discoveryWeights']).read_text())
        self.assertEqual(weights, {'version': 2, 'sources': {'hn': [1, 0], 'lesswrong': [2, 1]}})
        mentions = [mention for file in sorted((root / 'context').glob('*.json')) for mention in json.loads(file.read_text())]
        lw = [m for m in mentions if m.get('source') == 'lesswrong']
        self.assertTrue(all(m['id'].startswith('lesswrong:') for m in lw))
        self.assertTrue(any('?commentId=c2' in m['url'] for m in lw))
        self.assertTrue(all(m['text'] is None for m in lw))
        # A removed link disappears when that thread is imported again.
        with self.db:
            ingest_rows(self.db, [{'post': item('post2', htmlBody='No links'), 'comments': []}])
        self.assertEqual(self.db.execute('SELECT count(*) FROM source_mentions').fetchone()[0], 3)

    def test_redirects_move_both_sources_and_new_imports_use_known_aliases(self):
        with self.db:
            ingest(self.db, [{'id': 1, 'type': 'story', 'by': 'hn', 'url': 'https://en.wikipedia.org/wiki/Old_name'}])
            ingest_rows(self.db, [{'post': item('post1', title='Old_name'), 'comments': []}])
            merge_metadata(self.db, 'en:Old name', wiki_url('https://en.wikipedia.org/wiki/New_name'))
            ingest_rows(self.db, [{'post': item('post2', title='Old_name'), 'comments': []}])
        self.assertEqual(self.db.execute('SELECT count(*) FROM source_mentions WHERE article_id=?', ('en:New name',)).fetchone()[0], 2)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_popularity_uses_relative_rank_with_equal_ties(self):
        mapped = equivalent_linkers([1] * 80 + [5] * 10 + [20] * 10, [1] * 8 + [2, 3])
        self.assertEqual(mapped, {1: 1, 2: 5, 3: 20})

    def test_cached_canonicalization_drops_missing_targets_and_keeps_metadata_unloaded(self):
        from pipeline.topic_predictions import open_cache
        cache = self.root / 'topics.sqlite'
        predictions = open_cache(cache)
        with predictions:
            predictions.executemany('INSERT INTO predictions VALUES (?,?,?,?,?,?)', [
                ('en', 'Old name', 'New name', 'predicted', '{"STEM.Computing":0.9}', 123),
                ('en', 'No such page', 'No such page', 'missing', '{}', 123)])
        predictions.close()
        with self.db:
            ingest_rows(self.db, [{'post': item('p1', title='Old_name'), 'comments': []},
                                  {'post': item('p2', title='No_such_page'), 'comments': []}])
        self.assertEqual(canonicalize(self.db, cache, self.root / 'unused.sqlite'), {'redirects': 1, 'missing': 1})
        self.assertEqual(self.db.execute('SELECT article_id FROM source_mentions').fetchone()[0], 'en:New name')
        self.assertEqual(self.db.execute('SELECT enriched_at FROM articles WHERE id=?', ('en:New name',)).fetchone()[0], 0)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
