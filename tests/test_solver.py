import unittest

from app.solver import NoSolutionError, feasible_range, solve


class SolverTests(unittest.TestCase):
    def test_reverse_inferable_example(self):
        matrix = [[1, 2, 3], [4, 5, 6]]
        result = solve(matrix, 2)

        self.assertEqual(result.primary_cells, ((0, 0),))
        # A single primary cell cannot be protected with fewer than three
        # secondary cells in this small table.
        self.assertEqual(result.secondary_cells, ((0, 1), (1, 0), (1, 1)))
        self.assertEqual(result.row_totals, (6, 15))
        self.assertEqual(result.column_totals, (5, 7, 9))
        self.assertEqual(
            [(r.min_feasible, r.max_feasible) for r in result.primary_ranges],
            [(0, 3)],
        )
        self.assertTrue(all(r.protected for r in result.primary_ranges))

    def test_multiple_primary_cells(self):
        matrix = [[1, 2], [0, 1]]
        result = solve(matrix, 2)

        self.assertEqual(result.primary_cells, ((0, 0), (1, 1)))
        self.assertEqual(result.secondary_cells, ((0, 1), (1, 0)))
        self.assertEqual(
            [(r.row, r.column) for r in result.primary_ranges],
            [(0, 0), (1, 1)],
        )
        self.assertTrue(all(r.protected for r in result.primary_ranges))

    def test_no_additional_suppression_needed(self):
        matrix = [[1, 1], [1, 1]]
        result = solve(matrix, 2)
        self.assertEqual(result.secondary_cells, ())
        self.assertEqual(len(result.primary_cells), 4)

    def test_no_small_flows_means_matrix_unchanged(self):
        matrix = [[10, 20], [30, 40]]
        result = solve(matrix, 2)
        self.assertEqual(result.primary_cells, ())
        self.assertEqual(result.secondary_cells, ())
        self.assertEqual(result.hidden_cells, ())

    def test_no_solution_is_reported(self):
        # Column 1 total is 2, row 1 is all zero, and (0,1)=2 is the only
        # possible source of column 1 flow.  Hiding every other cell cannot
        # make the primary cell take a second value.
        with self.assertRaises(NoSolutionError) as raised:
            solve([[3, 2], [0, 0]], 3)
        self.assertEqual(raised.exception.unprotectable_cells, [(0, 1)])

    def test_large_k_makes_all_nonzero_cells_primary(self):
        matrix = [[1, 2], [3, 4]]
        result = solve(matrix, 10)
        self.assertEqual(result.secondary_cells, ())
        self.assertEqual(len(result.primary_cells), 4)
        self.assertEqual(len(result.hidden_cells), 4)

    def test_range_of_a_hidden_cell(self):
        # With every cell hidden for matrix [[1, 2], [3, 4]], (0,0) can be
        # any integer from 0 through 3.
        cell_range = feasible_range(
            m=2,
            n=2,
            hidden_edges=[(0, 0), (0, 1), (1, 0), (1, 1)],
            hidden_supplies=[3, 7],
            hidden_demands=[4, 6],
            cell=(0, 0),
            original_value=1,
        )
        self.assertEqual((cell_range.min_feasible, cell_range.max_feasible), (0, 3))
        self.assertEqual(cell_range.value_count, 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
