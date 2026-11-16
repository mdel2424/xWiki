// Strongly suppress the archive's large under-five-linker tail, while retaining
// a small discovery chance. Above five, favor broad HN interest with a soft cap.
export function discoveryWeight(linkers = 0) {
  const count = Math.min(100, Math.max(0, Number(linkers) || 0));
  return count < 5 ? .02 * Math.sqrt(Math.max(1, count)) : Math.pow(count / 5, .65);
}

export function randomValue(id, seed) {
  let hash = 2166136261;
  for (const char of `${seed}:${id}`) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
  hash ^= hash >>> 16; hash = Math.imul(hash, 0x7feb352d);
  hash ^= hash >>> 15; hash = Math.imul(hash, 0x846ca68b);
  // Strictly between zero and one, including for extreme hash values.
  return (((hash ^ hash >>> 16) >>> 0) + .5) / 4294967296;
}

export function communityWeights(data) {
  if (Array.isArray(data)) return data.map(discoveryWeight);
  const hn = data.sources.hn, lw = data.sources.lesswrong;
  const reference = hn.filter(n => n > 0).sort((a, b) => a - b);
  const values = lw.filter(n => n > 0).sort((a, b) => a - b), mapped = new Map();
  // Ties share the middle of their community's percentile band. Match that
  // percentile to HN before applying the existing low-interest penalty and cap.
  for (let start = 0; start < values.length;) {
    let end = start + 1;
    while (end < values.length && values[end] === values[start]) end++;
    const percentile = values.length > 1 ? (start + end - 1) / (2 * (values.length - 1)) : .5;
    mapped.set(values[start], reference.length ? reference[Math.min(reference.length - 1, Math.floor(percentile * (reference.length - 1)))] : values[start]);
    start = end;
  }
  const weights = [hn.map(n => n > 0 ? discoveryWeight(n) : 0), lw.map(n => n > 0 ? discoveryWeight(mapped.get(n)) : 0)];
  const totals = weights.map(source => source.reduce((sum, value) => sum + value, 0));
  const active = totals.filter(total => total > 0).length || 1;
  // Each available community owns the same total discovery mass. An article
  // endorsed by both gets both signals, but appears only once in the draw.
  return hn.map((_, i) => weights.reduce((sum, source, j) => sum + (totals[j] ? source[i] / totals[j] / active : 0), 0));
}

export function discoveryOrder(linkers, seed) {
  const weights = communityWeights(linkers);
  const keys = new Float64Array(weights.length);
  const order = new Uint32Array(weights.length);
  for (let index = 0; index < weights.length; index++) {
    order[index] = index;
    // An exponential race samples proportionally to weight, without replacement.
    keys[index] = weights[index] ? -Math.log(randomValue(index, `discovery:${seed}`)) / weights[index] : Infinity;
  }
  return order.sort((a, b) => keys[a] - keys[b] || a - b);
}
