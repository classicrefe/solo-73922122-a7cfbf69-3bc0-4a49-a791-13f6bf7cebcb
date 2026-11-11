"""Core cell-suppression solver.

The publication keeps every row and column total.  A cell is *hidden*
when its value is replaced by ``null``.  The primary hidden cells are the
small flows ``1 .. k-1``.  Secondary suppression is chosen with the minimum
number of extra cells so that every primary cell has at least two feasible
integer values in every non-negative matrix that agrees with the published
cells and the published row/column totals.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Sequence


class NoSolutionError(Exception):
    """Raised when even hiding every non-primary cell cannot protect a cell."""

    def __init__(self, message: str, unprotectable_cells: Sequence[tuple[int, int]]):
        super().__init__(message)
        self.message = message
        self.unprotectable_cells = sorted(unprotectable_cells)


@dataclass(frozen=True)
class CellRange:
    row: int
    column: int
    min_feasible: int
    max_feasible: int

    @property
    def value_count(self) -> int:
        # Feasible values on one edge of a transportation network form an
        # integer interval.  min/max therefore also describe the whole set.
        return self.max_feasible - self.min_feasible + 1

    @property
    def protected(self) -> bool:
        return self.max_feasible > self.min_feasible


class _Dinic:
    """Small integer max-flow implementation (Dinic, iterative DFS)."""

    __slots__ = ("n", "graph", "level")

    def __init__(self, n: int) -> None:
        self.n = n
        self.graph: list[list[list[int]]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, capacity: int) -> None:
        forward = [v, capacity, len(self.graph[v])]
        backward = [u, 0, len(self.graph[u])]
        self.graph[u].append(forward)
        self.graph[v].append(backward)

    def max_flow(self, source: int, sink: int, limit: int) -> int:
        if limit <= 0:
            return 0
        flow = 0
        n = self.n
        graph = self.graph

        while flow < limit:
            level = [-1] * n
            level[source] = 0
            queue = [source]
            for u in queue:
                for edge in graph[u]:
                    if edge[1] > 0 and level[edge[0]] < 0:
                        level[edge[0]] = level[u] + 1
                        queue.append(edge[0])
            if level[sink] < 0:
                break

            current = [0] * n

            def dfs(u: int, pushed: int) -> int:
                if u == sink:
                    return pushed
                while current[u] < len(graph[u]):
                    edge = graph[u][current[u]]
                    v, capacity, reverse_index = edge
                    if capacity > 0 and level[v] == level[u] + 1:
                        sent = dfs(v, min(pushed, capacity))
                        if sent:
                            edge[1] -= sent
                            graph[v][reverse_index][1] += sent
                            return sent
                    current[u] += 1
                return 0

            while flow < limit:
                sent = dfs(source, limit - flow)
                if not sent:
                    break
                flow += sent

        return flow


def _max_flow_value(
    m: int,
    n: int,
    hidden_edges: Sequence[tuple[int, int]],
    excluded: tuple[int, int] | None,
    supplies: Sequence[int],
    demands: Sequence[int],
) -> int:
    total = sum(supplies)
    if total == 0:
        return 0

    source = m + n
    sink = source + 1
    network = _Dinic(sink + 1)

    for i, supply in enumerate(supplies):
        if supply:
            network.add_edge(source, i, supply)
    for j, demand in enumerate(demands):
        if demand:
            network.add_edge(m + j, sink, demand)
    for edge in hidden_edges:
        if edge == excluded:
            continue
        i, j = edge
        # ``total`` is a safe infinity: no edge can carry more than the
        # total residual supply.
        network.add_edge(i, m + j, total)

    return network.max_flow(source, sink, total)


def _is_feasible(
    m: int,
    n: int,
    hidden_edges: Sequence[tuple[int, int]],
    hidden_supplies: Sequence[int],
    hidden_demands: Sequence[int],
    cell: tuple[int, int],
    value: int,
) -> bool:
    """Can the selected hidden cells form a matrix with ``cell == value``?"""

    row, column = cell
    if value < 0 or value > hidden_supplies[row] or value > hidden_demands[column]:
        return False

    supplies = list(hidden_supplies)
    demands = list(hidden_demands)
    supplies[row] -= value
    demands[column] -= value

    if any(v < 0 for v in supplies) or any(v < 0 for v in demands):
        return False
    if sum(supplies) != sum(demands):  # defensive; holds by construction
        return False

    total = sum(supplies)
    if total == 0:
        return True
    if total != sum(demands):
        return False

    return _max_flow_value(m, n, hidden_edges, cell, supplies, demands) == total


def feasible_range(
    m: int,
    n: int,
    hidden_edges: Sequence[tuple[int, int]],
    hidden_supplies: Sequence[int],
    hidden_demands: Sequence[int],
    cell: tuple[int, int],
    original_value: int,
) -> CellRange:
    """Return the exact minimum/maximum feasible value of one hidden cell.

    The feasible set of one edge in a transportation polytope is an integer
    interval.  We know that ``original_value`` is feasible (the input matrix
    itself witnesses it), so binary searches for both ends are valid.
    """

    row, column = cell
    upper = min(hidden_supplies[row], hidden_demands[column])

    lo, hi = 0, original_value
    while lo < hi:
        mid = (lo + hi) // 2
        if _is_feasible(
            m, n, hidden_edges, hidden_supplies, hidden_demands, cell, mid
        ):
            hi = mid
        else:
            lo = mid + 1
    minimum = lo

    lo, hi = original_value, upper
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _is_feasible(
            m, n, hidden_edges, hidden_supplies, hidden_demands, cell, mid
        ):
            lo = mid
        else:
            hi = mid - 1
    maximum = lo

    return CellRange(
        row=row,
        column=column,
        min_feasible=minimum,
        max_feasible=maximum,
    )


@dataclass(frozen=True)
class SolveResult:
    k: int
    primary_cells: tuple[tuple[int, int], ...]
    secondary_cells: tuple[tuple[int, int], ...]
    hidden_cells: tuple[tuple[int, int], ...]
    primary_ranges: tuple[CellRange, ...]
    row_totals: tuple[int, ...]
    column_totals: tuple[int, ...]


def _hidden_totals(
    matrix: Sequence[Sequence[int]],
    hidden_cells: set[tuple[int, int]],
) -> tuple[list[int], list[int]]:
    m = len(matrix)
    n = len(matrix[0])
    supplies = [0] * m
    demands = [0] * n
    for i, j in hidden_cells:
        supplies[i] += matrix[i][j]
        demands[j] += matrix[i][j]
    return supplies, demands


def _ranges_for_hidden_set(
    matrix: Sequence[Sequence[int]],
    primary_cells: Sequence[tuple[int, int]],
    hidden_cells: set[tuple[int, int]],
    stop_on_unprotected: bool = True,
) -> tuple[list[CellRange], list[tuple[int, int]]]:
    m = len(matrix)
    n = len(matrix[0])
    supplies, demands = _hidden_totals(matrix, hidden_cells)
    hidden_edges = sorted(hidden_cells)
    ranges: list[CellRange] = []
    unprotected: list[tuple[int, int]] = []

    for cell in primary_cells:
        cell_range = feasible_range(
            m,
            n,
            hidden_edges,
            supplies,
            demands,
            cell,
            matrix[cell[0]][cell[1]],
        )
        ranges.append(cell_range)
        if not cell_range.protected:
            unprotected.append(cell)
            if stop_on_unprotected:
                break

    return ranges, unprotected


def solve(matrix: Sequence[Sequence[int]], k: int) -> SolveResult:
    """Find a minimum-cardinality secondary suppression set.

    ``matrix`` must be a non-empty rectangular 2..3 by 2..4 integer matrix
    with entries in 0..10000 and ``k >= 2``.
    """

    if not matrix or not matrix[0]:
        raise ValueError("matrix must be non-empty")
    m, n = len(matrix), len(matrix[0])
    if not 2 <= m <= 3 or not 2 <= n <= 4:
        raise ValueError("matrix must have 2..3 rows and 2..4 columns")
    if any(len(row) != n for row in matrix):
        raise ValueError("matrix rows must have equal length")
    if k < 2:
        raise ValueError("k must be >= 2")
    for row in matrix:
        for value in row:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("matrix values must be integers")
            if not 0 <= value <= 10000:
                raise ValueError("matrix values must be in 0..10000")

    primary_cells = tuple(
        (i, j)
        for i in range(m)
        for j in range(n)
        if 1 <= matrix[i][j] <= k - 1
    )
    all_cells = {(i, j) for i in range(m) for j in range(n)}
    secondary_candidates = tuple(sorted(all_cells - set(primary_cells)))

    row_totals = tuple(sum(row) for row in matrix)
    column_totals = tuple(sum(matrix[i][j] for i in range(m)) for j in range(n))

    if not primary_cells:
        return SolveResult(
            k=k,
            primary_cells=(),
            secondary_cells=(),
            hidden_cells=(),
            primary_ranges=(),
            row_totals=row_totals,
            column_totals=column_totals,
        )

    # Search by increasing number of extra hidden cells.  This directly
    # implements "以最少格数补充隐藏其他单元".
    for count in range(0, len(secondary_candidates) + 1):
        for extra in combinations(secondary_candidates, count):
            hidden = set(primary_cells)
            hidden.update(extra)
            ranges, unprotected = _ranges_for_hidden_set(
                matrix, primary_cells, hidden, stop_on_unprotected=True
            )
            if not unprotected:
                # The early exit never triggered, so every primary range was
                # computed against this final hidden set.
                return SolveResult(
                    k=k,
                    primary_cells=primary_cells,
                    secondary_cells=tuple(extra),
                    hidden_cells=tuple(sorted(hidden)),
                    primary_ranges=tuple(ranges),
                    row_totals=row_totals,
                    column_totals=column_totals,
                )

    # No suppression set can protect every primary cell.  Report which cells
    # remain forced even when every other cell is hidden.
    hidden = set(all_cells)
    _, unprotected = _ranges_for_hidden_set(
        matrix, primary_cells, hidden, stop_on_unprotected=False
    )
    raise NoSolutionError(
        "无法在保留公开行列合计的前提下保护所有小客流单元",
        unprotected,
    )


def feasible_values_available(
    m: int,
    n: int,
    hidden_edges: Iterable[tuple[int, int]],
    hidden_supplies: Sequence[int],
    hidden_demands: Sequence[int],
    cell: tuple[int, int],
    original_value: int,
) -> CellRange:
    """Convenience wrapper used by tests and callers."""

    return feasible_range(
        m,
        n,
        tuple(sorted(hidden_edges)),
        hidden_supplies,
        hidden_demands,
        cell,
        original_value,
    )
