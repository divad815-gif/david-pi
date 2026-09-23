import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class PlacesExperienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (ROOT / "templates" / "places.html").read_text()
        cls.script = (ROOT / "static" / "places.js").read_text()
        cls.styles = (ROOT / "static" / "platform.css").read_text()

    def test_restaurant_list_has_complete_async_states(self):
        for marker in (
            'id="restaurantResults" aria-busy="false"',
            'id="restaurantsLoading"',
            'id="placesNoResults"',
            'id="placesError"',
            'id="placesRetry"',
            'id="restaurantResultCount"',
        ):
            self.assertIn(marker, self.template)
        self.assertIn("restaurantController?.abort()", self.script)
        self.assertIn("placesPanel.begin(generation)", self.script)
        self.assertIn("placesPanel.transition('error')", self.script)

    def test_margarita_list_has_loading_error_and_retry(self):
        for marker in ('id="margaritaResults" aria-busy="false"', 'id="margaritasLoading"', 'id="margaritasError"', 'id="margaritasRetry"'):
            self.assertIn(marker, self.template)
        self.assertIn("margaritaController?.abort()", self.script)
        self.assertIn("margaritasPanel.transition('error')", self.script)
        self.assertIn('id="margaritaMigrationNotice"', self.template)
        self.assertIn("item.migration_conflict", self.script)
        self.assertIn("edit.disabled = true", self.script)

    def test_margarita_photo_and_edit_are_separate_accessible_controls(self):
        self.assertIn("document.createElement('article'); card.className = 'marg-card'", self.script)
        self.assertIn("photoButton.className = 'marg-photo-open'", self.script)
        self.assertIn("edit.className = 'marg-edit'; edit.textContent = 'Edit'", self.script)
        self.assertIn("Open photo: ${margaritaPhotoCaption(item)}", self.script)
        self.assertIn("Edit ${item.month_name} margarita", self.script)
        self.assertIn("openMargaritaPhoto(item, photoButton)", self.script)
        self.assertIn("edit.addEventListener('click', () => openMarg(item))", self.script)
        self.assertNotIn("button.className = 'marg-card'", self.script)

    def test_margarita_conflict_reloads_current_shared_month_before_retry(self):
        self.assertIn("error.status = response.status", self.script)
        self.assertIn("error.data = data", self.script)
        self.assertIn("if (error.status === 409 && error.data?.conflict)", self.script)
        self.assertIn("const reloaded = await loadMargaritas()", self.script)
        self.assertIn("if (!reloaded)", self.script)
        self.assertIn("state.margaritas.find((item) => item.month === month)", self.script)
        self.assertIn("margaritaConflict", self.script)
        self.assertIn("Your draft and selected photo are still here.", self.script)
        self.assertIn('/static/places.js?v=14', self.template)

    def test_shared_place_viewer_handles_single_margarita_photo_accessibly(self):
        self.assertIn('aria-labelledby="placePhotoCaption"', self.template)
        self.assertIn('id="placePhotoCaption"', self.template)
        self.assertIn("$('#previousPlacePhoto').hidden = !hasMultiple", self.script)
        self.assertIn("$('#nextPlacePhoto').hidden = !hasMultiple", self.script)
        self.assertIn("$('#placePhotoCount').hidden = !hasMultiple", self.script)
        self.assertIn("$('#placePhotoViewer').addEventListener('close'", self.script)
        self.assertIn("if (trigger?.isConnected) trigger.focus()", self.script)
        self.assertIn("$('#closePlacePhotos').focus()", self.script)
        self.assertIn("object-fit:contain", self.styles)
        self.assertIn(".place-photo-nav[hidden]{display:none}", self.styles)
        self.assertIn("`${item.image_url}${item.image_url.includes", self.script)

    def test_tabs_and_recoverable_trash_copy_are_unambiguous(self):
        self.assertIn('data-tab="want_to_go" aria-pressed="true"', self.template)
        self.assertIn("DavidPiFilterGroup.create('#placesTabs'", self.script)
        self.assertIn('data-tab="trash"', self.template)
        self.assertIn('remain recoverable for at least 30 days.', self.template)
        self.assertNotIn('This cannot be undone.', self.template)

    def test_date_night_has_one_household_scope_and_no_private_selector(self):
        self.assertIn(
            'Restaurants, photos, and reviews here are shared with Household.',
            self.template,
        )
        self.assertNotIn('id="restaurantVisibility"', self.template)
        self.assertNotIn('Who can see this?', self.template)
        self.assertNotIn('Only me', self.template)
        self.assertNotIn("$('#restaurantVisibility')", self.script)
        self.assertNotIn("form.append('visibility'", self.script)


if __name__ == "__main__":
    unittest.main()
