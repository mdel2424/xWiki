import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, mkdtemp, rm } from 'node:fs/promises';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { StaticCatalog } from '../src/static-catalog.js';
import { sourceLabel, interestLinkers } from '../src/communities.js';
import { feedback, emptyProfile } from '../src/recommendations.js';
const exec = promisify(execFile);

test('mixed catalogue supports search, topic discovery and merged context without contaminating HN sorts', async () => {
  const temporary = await mkdtemp(path.join(tmpdir(), 'xwiki-lesswrong-'));
  try {
    await exec('python3', ['-c', `
import sys,json,time
from pipeline.store import connect,ingest,refresh_stats,merge_metadata,wiki_url
from pipeline.lesswrong import ingest_rows
from pipeline.static_export import export_static
from pipeline.discovery import refresh_discovery
from pipeline.ranking import refresh_rising
c=connect(sys.argv[1]+'/fixture.sqlite')
now=int(time.time())
with c:
 ingest(c,[{'id':1,'type':'story','by':'hn','time':now,'url':'https://en.wikipedia.org/wiki/Earth'}])
 for title,identity in [('Earth','lw1'),('Nashville Number System','lw2')]:
  ingest_rows(c,[{'post':{'_id':identity,'title':'LW thread','postedAt':'2026-05-01T00:00:00Z',
   'user':{'_id':'lw-author','displayName':'LW person'},'htmlBody':'<a href="https://en.wikipedia.org/wiki/'+title.replace(' ','_')+'">link</a>'},'comments':[]}])
  merge_metadata(c,'en:'+title,{**wiki_url('https://en.wikipedia.org/wiki/'+title.replace(' ','_')),
   'topicPredictions':{'Culture.Media.Music':.9}})
 refresh_stats(c)
m=export_static(c,sys.argv[1]+'/catalog',article_size=1,context_size=1)
refresh_discovery(sys.argv[1]+'/catalog')
refresh_rising(sys.argv[1]+'/catalog',now)
c.close()
`, temporary]);
    const base = new URL('https://example.test/xWiki/data/catalog/');
    const catalog = new StaticCatalog({ base, fetcher: async raw => ({ ok: true,
      json: async () => JSON.parse(await readFile(path.join(temporary, 'catalog', new URL(raw).pathname.slice(base.pathname.length)), 'utf8')) }) });
    await catalog.open();
    const top = await catalog.feed({ sort: 'top' });
    assert.deepEqual(top.articles.map(a => a.title), ['Earth']);
    assert.deepEqual((await catalog.feed({ sort: 'rising' })).articles.map(a => a.title), ['Earth']);
    const search = await catalog.search('Nashville');
    assert.equal(search.articles.length, 1);
    assert.equal(sourceLabel(search.articles[0]), 'LessWrong');
    const feed = await catalog.feed({ mode: 'arts', mix: 42 });
    assert.equal(new Set(feed.articles.map(a => a.id)).size, 2);
    assert.ok(feed.articles.every(a => a.topics.includes('arts')));
    const first = await catalog.article('en:Earth', { limit: 1 });
    const next = await catalog.article('en:Earth', { cursor: first.nextCursor, limit: 1 });
    assert.equal(first.mentions.length + next.mentions.length, 2);
    assert.equal(next.nextCursor, null);
    const lw = [...first.mentions, ...next.mentions].find(m => m.source === 'lesswrong');
    assert.equal(lw.url, 'https://www.lesswrong.com/posts/lw1');
    assert.equal(lw.text, null);
    assert.equal(first.sourceMetrics.hn.distinctLinkers, 1);
    assert.equal(first.sourceMetrics.lesswrong.distinctLinkers, 1);
    const saved = feedback(emptyProfile(), search.articles[0], 'like');
    assert.equal(sourceLabel(saved.likes[search.articles[0].id]), 'LessWrong');
    assert.equal(interestLinkers(saved.likes[search.articles[0].id]), interestLinkers(search.articles[0]));
  } finally { await rm(temporary, { recursive: true, force: true }); }
});

test('community popularity survives caching and does not receive an extra HN-only readership penalty', async () => {
  const { readershipWeight } = await import('../src/readership.js');
  const { rankArticles, mergeArticles } = await import('../src/core.js');
  const now = Date.now();
  const traffic = { dailyViews: 0, days: 30, checkedAt: now };
  const lw = { id: 'en:LW', title: 'LW', mentions: [], metrics: { distinctLinkers: 2 }, readership: traffic,
    sourceMetrics: { lesswrong: { distinctLinkers: 2, normalizedLinkers: 20, mentionCount: 2 } } };
  const hn = { id: 'en:HN', title: 'HN', mentions: [], metrics: { distinctLinkers: 20 }, readership: traffic,
    sourceMetrics: { hn: { distinctLinkers: 20, mentionCount: 20 } } };
  assert.equal(readershipWeight(lw, now), readershipWeight(hn, now));
  assert.deepEqual(rankArticles([lw, hn], 'top').map(a => a.id), ['en:HN']);
  const updated = mergeArticles([lw], [{ ...hn, id: lw.id }])[0];
  assert.equal(sourceLabel(updated), 'HN', 'A newly published source record replaces stale cached attribution');
});

test('a topic or collection with only LessWrong candidates fills missing HN featured slots with discoveries', async () => {
  const count = 30;
  const cards = Array.from({ length: count }, (_, i) => ({ id: `en:LW${i}`, title: `LW${i}`, lang: 'en', _index: i, mentions: [] }));
  const catalog = new StaticCatalog({ base: new URL('https://example.test/catalog/'), fetcher: async raw => ({ ok: true, json: async () => {
    const name = new URL(raw).pathname;
    if (name.endsWith('manifest.json')) return { kind: 'wikihack-static', version: 1, generation: 'v-lw', articleCount: count,
      hnArticleCount: 0, articleShardSize: 256, risingCount: 0, discoveryWeights: 'weights.json' };
    if (name.endsWith('weights.json')) return { version: 2, sources: { hn: Array(count).fill(0), lesswrong: Array(count).fill(1) } };
    if (name.endsWith('articles/0.json')) return cards;
    throw new Error(`Unexpected request: ${name}`);
  } }) });
  await catalog.open();
  const first = await catalog.feed({ limit: 10, mix: 4 });
  assert.equal(first.articles.length, 10);
  assert.equal(first.sources.random.length, 10);
  assert.equal(first.sources.top.length + first.sources.rising.length, 0);
  const next = await catalog.feed({ limit: 10, mix: 4, cursor: first.nextCursor, exclude: first.articles.map(a => a.id) });
  assert.equal(next.articles.length, 10);
  assert.equal(new Set([...first.articles, ...next.articles].map(a => a.id)).size, 20);
});
