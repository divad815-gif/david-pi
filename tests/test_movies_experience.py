import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class MovieExperienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (ROOT / "templates" / "movies.html").read_text()
        cls.script = (ROOT / "static" / "movies.js").read_text()

    def test_watchlist_has_explicit_async_states_and_retry(self):
        for marker in (
            'id="moviesPanel" aria-busy="false"',
            'id="moviesLoading"',
            'id="moviesNoResults"',
            'id="moviesError"',
            'id="moviesRetry"',
            'id="movieResultCount"',
            'id="moviesMore" hidden',
        ):
            self.assertIn(marker, self.template)
        self.assertIn("moviesPanel.begin(generation)", self.script)
        self.assertIn("moviesPanel.transition('error')", self.script)
        self.assertIn("activeListController?.abort()", self.script)

    def test_watchlist_is_paginated_without_hiding_loaded_cards(self):
        self.assertIn("const pageSize = 24", self.script)
        self.assertIn("limit:pageSize, offset", self.script)
        self.assertIn("if (append) grid.append(...cards)", self.script)
        self.assertIn("moviesMore.addEventListener('click'", self.script)
        self.assertIn("data.available_count", self.script)

    def test_search_result_has_only_one_interactive_control(self):
        search_start = self.script.index("async function searchMovies")
        search_end = self.script.index("async function addMovie", search_start)
        search = self.script[search_start:search_end]
        self.assertIn("add.setAttribute('aria-label'", search)
        self.assertNotIn("row.setAttribute('role', 'button')", search)
        self.assertNotIn("row.tabIndex", search)
        self.assertNotIn("row.addEventListener('click'", search)

    def test_filters_expose_pressed_state_and_use_shared_control(self):
        self.assertIn('data-type="movie" aria-pressed="true"', self.template)
        self.assertIn('data-view="all" aria-pressed="true"', self.template)
        self.assertIn("DavidPiFilterGroup.create('#mediaTypeTabs'", self.script)
        self.assertIn("DavidPiFilterGroup.create('#movieViews'", self.script)


if __name__ == "__main__":
    unittest.main()
