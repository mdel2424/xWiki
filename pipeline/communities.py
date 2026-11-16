"""Map community-relative popularity to comparable HN-equivalent linker counts."""
from bisect import bisect_left, bisect_right


def equivalent_linkers(reference, counts):
    reference = sorted(count for count in reference if count > 0)
    values = sorted(count for count in counts if count > 0)
    if not reference or not values:
        return {count: count for count in values}
    result = {}
    for count in set(values):
        percentile = ((bisect_left(values, count) + bisect_right(values, count) - 1) / (2 * (len(values) - 1))) if len(values) > 1 else .5
        result[count] = reference[min(len(reference) - 1, int(percentile * (len(reference) - 1)))]
    return result
