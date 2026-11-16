// Community IDs stay separate even when two people share a display name.
export function sourceMetrics(article) {
  if (article.sourceMetrics) return article.sourceMetrics;
  return { hn: article.metrics || { distinctLinkers: new Set((article.mentions || []).filter(m => !m.source || m.source === 'hn').map(m => m.author)).size } };
}
export function interestLinkers(article) {
  const sources = sourceMetrics(article);
  return Math.max(sources.hn?.distinctLinkers || 0, sources.lesswrong?.normalizedLinkers ?? sources.lesswrong?.distinctLinkers ?? 0);
}
export function sourceLabel(article) {
  const sources = sourceMetrics(article);
  return [sources.hn ? 'HN' : '', sources.lesswrong ? 'LessWrong' : ''].filter(Boolean).join(' & ') || 'HN';
}

export function dominantSource(article) {
  const sources = sourceMetrics(article);
  return sources.lesswrong && (!sources.hn || sources.lesswrong.distinctLinkers > sources.hn.distinctLinkers) ? 'lesswrong' : 'hn';
}
