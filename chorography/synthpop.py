"""Synthetic population construction for Chorography.

The synthesis is sample-free. No microdata sample is used. A seed table
over the cross product of the modelled dimensions starts uniform and is
fitted to the published marginal totals of each dimension by iterative
proportional fitting (IPF). Each fitted cell becomes one composite
household agent whose weight is the fitted cell total.

A composite can be expanded into individual agents under a seeded random
stream, and individuals can be collapsed back into composites by
aggregation over their synthesis cell. Expansion and collapse conserve
total weight exactly up to floating point summation order, which the test
suite pins down.

At national scale the fitted cross product of one county can hold more
cells than a region of thousands of counties can afford composites, so
build_composites accepts a per tract cell target. Cells above the target
are merged smallest first into their nearest neighbours by merge_cells,
which conserves the total weight exactly and keeps every composite's
weight the true number of households it represents. Regions that set no
target get the unmerged table, exactly as before.
"""

import itertools

from .agents import Household


def ipf(marginals, max_iter=1000, tol=1e-9):
    """Fit a multi-way table to one-way marginals.

    marginals maps a dimension name to a dict of category to target total.
    Returns (table, iterations, max_error) where table maps a tuple of
    categories, in the dimension order of marginals, to a fitted weight.
    All marginal totals are first rescaled to their mean so that slightly
    inconsistent published totals do not prevent convergence.
    """
    dims = list(marginals.keys())
    cats = [list(marginals[d].keys()) for d in dims]
    totals = [sum(marginals[d].values()) for d in dims]
    for d, total in zip(dims, totals):
        if total <= 0:
            raise ValueError(
                "marginal for dimension {} has no positive total".format(d))
    grand = sum(totals) / len(totals)
    targets = {}
    for d in dims:
        scale = grand / sum(marginals[d].values())
        targets[d] = {c: v * scale for c, v in marginals[d].items()}
    table = {}
    for key in itertools.product(*cats):
        table[key] = 1.0
    max_error = None
    for iteration in range(1, max_iter + 1):
        for di, d in enumerate(dims):
            sums = {c: 0.0 for c in cats[di]}
            for key, value in table.items():
                sums[key[di]] += value
            for key in list(table.keys()):
                current = sums[key[di]]
                if current > 0:
                    table[key] = table[key] * targets[d][key[di]] / current
        max_error = 0.0
        for di, d in enumerate(dims):
            sums = {c: 0.0 for c in cats[di]}
            for key, value in table.items():
                sums[key[di]] += value
            for c in cats[di]:
                err = abs(sums[c] - targets[d][c])
                if err > max_error:
                    max_error = err
        if max_error <= tol * max(grand, 1.0):
            return table, iteration, max_error
    return table, max_iter, max_error


def merge_cells(table, target, size_pos=None, cats=None,
                size_values=None):
    """Merge fitted cells into neighbours until at most target remain.

    A merge moves the whole weight of one cell onto a surviving
    neighbour cell differing in exactly one dimension. Total weight
    is conserved exactly, because weight is only ever moved, never
    created or dropped.

    The choice of cells protects the household size distribution,
    because represented people are weight times size and a merge
    across sizes silently re-counts one cell's households at
    another size. While any cell still has a neighbour of the same
    size, merges happen only between same size cells: the smallest
    such cell folds into its largest same size neighbour. A cross
    size merge is considered only when every size present is down
    to a single cell, which a target at or above the number of size
    categories never forces, and then the neighbour of closest size
    wins, with size_values giving each size category's numeric
    size. All choices are deterministic: scans run in sorted key
    order and ties keep the first candidate.

    Regions at sample scale never call this. It exists for regions
    whose fitted cross product would otherwise produce more
    composites than a tick loop can carry, where the count of
    composites must be traded against cell resolution while the
    weight each composite carries stays the true count of households
    it represents.
    """
    cells = dict(table)
    if not cells or len(cells) <= target:
        return cells

    def neighbours(key):
        found = []
        if cats is not None:
            for pos, options in enumerate(cats):
                for option in options:
                    if option == key[pos]:
                        continue
                    candidate = key[:pos] + (option,) + key[pos + 1:]
                    if candidate in cells:
                        found.append(candidate)
        else:
            for other in cells:
                if other == key:
                    continue
                if sum(1 for a, b in zip(other, key) if a != b) == 1:
                    found.append(other)
        return found

    def same_size(key, options):
        if size_pos is None:
            return options
        return [other for other in options
                if other[size_pos] == key[size_pos]]

    def largest(options):
        best = None
        for other in sorted(options):
            if best is None or cells[other] > cells[best]:
                best = other
        return best

    while len(cells) > target:
        smallest = None
        best = None
        for key in sorted(cells):
            options = same_size(key, neighbours(key))
            if not options:
                continue
            if smallest is None or cells[key] < cells[smallest]:
                smallest = key
                best = largest(options)
        if smallest is None:
            for key in sorted(cells):
                if smallest is None or cells[key] < cells[smallest]:
                    smallest = key
            options = neighbours(smallest)
            if options:
                def distance(other):
                    if size_pos is None or not size_values:
                        return 0
                    return abs(size_values.get(other[size_pos], 0)
                               - size_values.get(smallest[size_pos], 0))
                best = None
                best_rank = None
                for other in sorted(options):
                    rank = (distance(other), -cells[other])
                    if best_rank is None or rank < best_rank:
                        best = other
                        best_rank = rank
            else:
                best = largest([key for key in cells if key != smallest])
        cells[best] += cells[smallest]
        del cells[smallest]
    return cells


def _income_index(band, ordered_bands):
    if band not in ordered_bands:
        return 0.5
    if len(ordered_bands) == 1:
        return 0.5
    return ordered_bands.index(band) / (len(ordered_bands) - 1)


def build_composites(tract_id, marginals, start_id, size_categories,
                     income_bands, target_cells=None):
    """Build the composite households of one tract from its marginals.

    With target_cells set, the fitted table is first merged down to
    at most that many cells by merge_cells, conserving total weight.
    Without it the fitted table is used exactly as it comes from the
    IPF, which is the behaviour every region up to national scale
    relies on.
    """
    table, _iterations, _err = ipf(marginals)
    dims = list(marginals.keys())
    if target_cells is not None:
        live = {key: value for key, value in table.items()
                if value > 1e-9}
        if len(live) > target_cells:
            size_pos = dims.index("size") if "size" in dims else None
            cats = [list(marginals[dim].keys()) for dim in dims]
            size_values = None
            if size_pos is not None:
                size_values = {}
                for category in cats[size_pos]:
                    if str(category).isdigit():
                        size_values[category] = int(category)
                    elif size_categories and category in size_categories:
                        size_values[category] = size_categories[category]
                    else:
                        size_values[category] = 1
            table = merge_cells(live, target_cells, size_pos=size_pos,
                                cats=cats, size_values=size_values)
    households = []
    next_id = start_id
    for key in sorted(table.keys()):
        weight = table[key]
        if weight <= 1e-9:
            continue
        attrs = dict(zip(dims, key))
        size = 1
        raw_size = attrs.get("size", "1")
        if str(raw_size).isdigit():
            size = int(raw_size)
        elif size_categories and raw_size in size_categories:
            size = size_categories[raw_size]
        band = attrs.get("income", income_bands[0] if income_bands else "mid")
        households.append(Household(
            id=next_id,
            tract=tract_id,
            cell_key="|".join(str(k) for k in key),
            weight=weight,
            size=size,
            income_band=band,
            tenure=attrs.get("tenure", "owner"),
            income_index=_income_index(band, income_bands),
            employment=0.45 + 0.45 * _income_index(band, income_bands),
            health=0.62 + 0.10 * _income_index(band, income_bands),
            morale=0.55 + 0.10 * _income_index(band, income_bands),
        ))
        next_id += 1
    return households, next_id


def total_weight(households):
    return sum(h.weight for h in households)


def total_people(households):
    return sum(h.weight * h.size for h in households)


def expand_composite(household, rng, start_id):
    """Expand one composite into individual agents under a seeded stream.

    The composite weight W becomes k individuals of weight W / k, where k
    is the rounded weight clamped to at least one. Behavioural fields get
    a small seeded jitter so individuals are not clones; the synthesis
    cell key is preserved on every individual so collapse can recover the
    composite exactly in weight.
    """
    k = int(round(household.weight))
    if k < 1:
        k = 1
    each = household.weight / k
    people = []
    for i in range(k):
        people.append(Household(
            id=start_id + i,
            tract=household.tract,
            cell_key=household.cell_key,
            weight=each,
            size=household.size,
            income_band=household.income_band,
            tenure=household.tenure,
            income_index=household.income_index,
            employment=_clamp01(household.employment + (rng.random() - 0.5) * 0.1),
            health=_clamp01(household.health + (rng.random() - 0.5) * 0.1),
            morale=_clamp01(household.morale + (rng.random() - 0.5) * 0.1),
            savings_months=household.savings_months,
            individual=True,
        ))
    return people


def collapse_households(households, start_id):
    """Aggregate individual agents back into composites by synthesis cell.

    Weights are summed and behavioural fields are weight-averaged, so the
    total weight of the collapsed set equals the total weight of the
    input set up to floating point summation.
    """
    groups = {}
    for h in households:
        groups.setdefault((h.tract, h.cell_key), []).append(h)
    composites = []
    next_id = start_id
    for key in sorted(groups.keys()):
        members = groups[key]
        w = sum(m.weight for m in members)
        if w <= 0:
            continue
        first = members[0]
        composites.append(Household(
            id=next_id,
            tract=key[0],
            cell_key=key[1],
            weight=w,
            size=first.size,
            income_band=first.income_band,
            tenure=first.tenure,
            income_index=first.income_index,
            employment=sum(m.employment * m.weight for m in members) / w,
            health=sum(m.health * m.weight for m in members) / w,
            morale=sum(m.morale * m.weight for m in members) / w,
            savings_months=sum(m.savings_months * m.weight for m in members) / w,
            individual=False,
        ))
        next_id += 1
    return composites


def _clamp01(value):
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value
