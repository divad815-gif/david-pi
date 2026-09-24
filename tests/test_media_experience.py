from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MediaExperienceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (ROOT / "templates" / "photos.html").read_text(encoding="utf-8")
        cls.script = (ROOT / "static" / "gallery.js").read_text(encoding="utf-8")
        cls.gesture_script = (ROOT / "static" / "gallery-density-gesture.js").read_text(encoding="utf-8")
        cls.window_script = (ROOT / "static" / "gallery-window.js").read_text(encoding="utf-8")
        cls.styles = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        cls.media_styles = (ROOT / "static" / "media-gallery.css").read_text(encoding="utf-8")

    def test_gallery_announces_concise_status_instead_of_every_inserted_card(self):
        self.assertIn('id="galleryStatus" role="status" aria-live="polite"', self.template)
        self.assertIn('id="gallery" aria-busy="true"', self.template)
        self.assertNotIn('id="gallery" aria-live=', self.template)
        self.assertIn("gallery.setAttribute('aria-busy', 'false')", self.script)

    def test_loading_and_retry_states_are_present(self):
        self.assertIn('id="gallerySkeleton"', self.template)
        self.assertIn('id="retryGallery"', self.template)
        self.assertIn('id="galleryPageError" role="status" aria-live="polite"', self.template)
        self.assertIn('id="retryGalleryPage"', self.template)
        self.assertIn("retryGallery.addEventListener('click', loadInitialGallery)", self.script)
        self.assertIn("retryGalleryPage.addEventListener('click'", self.script)
        self.assertIn("error?.code === 'gallery_cursor_repeated'", self.script)
        self.assertIn(": loadedPhotos.length ? 'page' : 'initial',", self.script)
        initial = self.script[self.script.index('async function loadInitialGallery()'):self.script.index("retryGallery.addEventListener", self.script.index('async function loadInitialGallery()'))]
        self.assertIn('const collectionsRequest = loadCollections()', initial)
        self.assertIn('let photosLoaded = await refreshPhotos()', initial)
        self.assertNotIn('loadTimeline(', initial)
        self.assertIn("mediaTimeline.addEventListener('toggle'", self.script)
        self.assertIn('if (!mediaTimeline.open) return false', self.script)
        self.assertIn("showGalleryLoadError('initial')", self.script)
        self.assertIn("if (!retryGallery.hidden && loadedPhotos.length === 0)", self.script)
        self.assertIn("galleryResultCount.textContent = 'Unavailable'", self.script)
        self.assertIn('galleryPageError.hidden = false', self.script)
        self.assertIn('.gallery-skeleton[hidden]', self.styles)

    def test_result_context_and_empty_actions_are_explicit(self):
        self.assertIn('id="galleryTitle"', self.template)
        self.assertIn('id="galleryResultCount"', self.template)
        self.assertIn('id="emptyBrowseAll"', self.template)
        self.assertIn('function configureEmptyState()', self.script)
        self.assertIn("emptyBrowseAll.textContent = 'Show all dates'", self.script)
        self.assertIn("emptyBrowseAll.textContent = 'Browse all media'", self.script)

    def test_page_metadata_is_windowed_and_aborts_stale_requests(self):
        self.assertIn('galleryWindowApi.createWindowManager({', self.script)
        self.assertIn('galleryWindowManager.setModel(galleryWindowModel(), {preserveRows})', self.script)
        self.assertIn('loadedPhotos.push(...additions)', self.script)
        self.assertIn('galleryAbortController?.abort()', self.script)
        self.assertIn("{signal: galleryAbortController?.signal}", self.script)
        self.assertIn("if (generation === galleryGeneration)", self.script)

    def test_first_viewport_thumbnails_receive_bounded_priority(self):
        self.assertIn('galleryWindowApi.thumbnailHintsForRow(', self.script)
        self.assertIn('image.loading = requestHints.loading', self.script)
        self.assertIn('image.fetchPriority = requestHints.fetchPriority', self.script)
        self.assertIn("return {loading: 'lazy', fetchPriority: 'low', visible: false}", self.window_script)
        self.assertIn("fetchPriority: firstVisiblePhotoRow && columnIndex < 3 ? 'high' : 'auto'", self.window_script)
        hint_block = self.script[self.script.index('const requestHints ='):self.script.index('button.append(image)', self.script.index('const requestHints ='))]
        self.assertLess(hint_block.index('image.loading ='), hint_block.index('image.src ='))
        self.assertLess(hint_block.index('image.fetchPriority ='), hint_block.index('image.src ='))
        self.assertIn('image.width = 320', self.script)
        self.assertIn('image.width = 284', self.script)
        self.assertIn("fallback.textContent = 'Preview unavailable'", self.script)
        self.assertIn("cleanUrl.searchParams.delete('collection')", self.script)

    def test_mobile_album_rail_stays_visible_and_links_to_complete_collection_page(self):
        self.assertIn('/static/media-gallery.css?v=16', self.template)
        media_styles = (ROOT / 'static' / 'media-gallery.css').read_text(encoding='utf-8')
        self.assertIn('@media (max-width: 760px)', media_styles)
        self.assertIn('.collection-navigator { display: block; }', media_styles)
        self.assertIn('.collection-rail { padding: 2px 0 12px; }', media_styles)
        self.assertIn('.media-owner-tabs button.selected', media_styles)
        self.assertIn('.collection-heading-actions {', media_styles)
        self.assertIn('href="/photos/collections">View all</a>', self.template)
        self.assertNotIn('class="collection-picker"', self.template)

    def test_bounded_selection_is_labeled_honestly(self):
        self.assertIn("totalPhotos > 500 ? 'Select first 500' : 'Select all'", self.script)
        self.assertIn("loadedPhotos.length < 500", self.script)

    def test_nonowners_keep_view_and_organize_without_destructive_controls(self):
        self.assertIn("deletePhoto.hidden = !photo.is_mine", self.script)
        self.assertIn("customActions.hidden = !selected || !selected.is_mine", self.script)
        self.assertIn("batchDelete.hidden = !onlyOwned", self.script)

    def test_structured_api_errors_render_human_message(self):
        self.assertIn("typeof result?.error?.message === 'string'", self.script)
        self.assertIn("apiErrorMessage(result, 'Something went wrong.')", self.script)

    def test_failed_large_viewer_image_clears_loading_state(self):
        start = self.script.index('async function upgradeViewerImage')
        end = self.script.index('async function upgradeViewerPoster', start)
        implementation = self.script[start:end]
        self.assertIn('if (ready && viewerRenditionRank < 2)', implementation)
        self.assertIn("viewerStage.classList.remove('loading');", implementation)

    def test_keyboard_focus_and_toggle_semantics_are_explicit(self):
        self.assertIn(':focus-visible', self.styles)
        self.assertIn('aria-pressed="true">All visible</button>', self.template)
        self.assertIn("removeAttribute('aria-pressed')", self.script)

    def test_search_is_removed_and_smart_views_remain_accessible(self):
        self.assertNotIn('id="mediaSearch"', self.template)
        self.assertNotIn('/api/photos/search', self.script)
        self.assertIn('data-kind="photo"', self.template)
        self.assertIn('data-kind="video"', self.template)
        self.assertIn('data-favorite="true"', self.template)
        self.assertIn('min-height: 46px;', self.media_styles)

    def test_captions_render_as_text_and_owner_only_editor_is_labeled(self):
        self.assertIn('id="viewerCaptionText"', self.template)
        self.assertIn('label for="viewerCaption">Caption</label>', self.template)
        self.assertIn('maxlength="1000"', self.template)
        self.assertIn("viewerCaptionText.textContent = photo.caption", self.script)
        self.assertIn("viewerCaption.value = photo.caption", self.script)
        self.assertNotIn('viewerCaptionText.innerHTML', self.script)
        self.assertIn("viewerCaptionEditor.hidden = !photo.is_mine", self.script)
        self.assertIn('.viewer-caption-editor textarea:focus', self.media_styles)
        self.assertIn('id="viewerCaptionToggle"', self.template)
        self.assertIn('aria-expanded="false" aria-controls="viewerDetails"', self.template)
        self.assertIn('id="viewerDetails" hidden', self.template)
        self.assertIn('id="viewerCaptionPreview" hidden', self.template)
        self.assertIn("viewerCaptionPreview.textContent=hasCaption?photo.caption:''", self.script)
        self.assertIn('setViewerCaptionExpanded(false);', self.script)
        self.assertIn('.viewer-details[hidden] { display: none; }', self.media_styles)

    def test_favorite_and_viewer_controls_are_responsive(self):
        self.assertIn('id="viewerFavorite" type="button" aria-pressed="false"', self.template)
        self.assertIn("viewerFavorite.setAttribute('aria-pressed'", self.script)
        self.assertIn("favorite.className = 'photo-favorite'", self.script)
        self.assertIn('.photo-favorite {', self.media_styles)
        self.assertIn('grid-template-columns: repeat(3, minmax(0, 1fr));', self.media_styles)
        self.assertIn('@media (prefers-reduced-motion: reduce)', self.media_styles)

    def test_all_visible_collection_rail_merges_shared_and_owned_collections(self):
        start = self.script.index('async function loadCollections()')
        end = self.script.index('function selectCollection', start)
        implementation = self.script[start:end]
        self.assertIn("requestedOwnerView === 'visible'", implementation)
        self.assertIn("? ['', 'mine']", implementation)
        self.assertIn('await Promise.all(collectionViews.map', implementation)
        self.assertIn('const collectionMap = new Map()', implementation)
        self.assertIn('collectionMap.set(collection.id, collection)', implementation)

    def test_media_dialogs_have_accessible_names(self):
        self.assertIn('id="uploadSheet" aria-labelledby="uploadTitle"', self.template)
        self.assertIn('id="viewer" aria-label="Media viewer"', self.template)
        self.assertIn('id="collectionEditor" aria-labelledby="collectionEditorTitle"', self.template)
        self.assertIn('id="confirmSheet" aria-labelledby="confirmTitle"', self.template)

    def test_closed_viewer_does_not_paint_or_force_horizontal_overflow(self):
        self.assertIn('/static/app.css?v=40', self.template)
        self.assertIn('/static/media-gallery.css?v=16', self.template)
        self.assertIn('.viewer[open] {', self.media_styles)
        self.assertNotIn('\n.viewer {\n  display: grid;', self.media_styles)
        self.assertIn('.viewer { width: 100%;', self.styles)
        self.assertNotIn('.viewer { width: 100vw;', self.styles)

    def test_mobile_media_controls_keep_touch_sized_targets(self):
        self.assertIn('.gallery-density button {', self.media_styles)
        self.assertIn('width: 44px;', self.media_styles)
        self.assertNotIn("controls.className = 'gallery-month-zoom'", self.script)
        self.assertIn('.timeline-heading button { flex: none; min-height: 44px;', self.styles)
        self.assertNotIn('.collection-heading-actions .collection-new { min-height: 38px;', self.styles)
        self.assertIn('.collection-heading-actions .collection-new {\n    min-height: 44px;', self.media_styles)

    def test_recently_deleted_count_uses_the_supported_visibility_scope(self):
        self.assertIn('/static/gallery.js?v=62', self.template)
        self.assertIn('/api/photos/deleted?limit=1&scope=${encodeURIComponent(requestedOwnerView)}', self.script)
        self.assertNotIn('/api/photos/deleted?limit=1&view=${encodeURIComponent(requestedOwnerView)}', self.script)

    def test_grid_and_viewer_zoom_are_global_progressive_and_persistent(self):
        self.assertIn('id="gridZoomOut"', self.template)
        self.assertIn('id="gridZoomIn"', self.template)
        self.assertIn('id="viewerZoomControls"', self.template)
        self.assertIn("localStorage.getItem('davidPiGalleryDensityV2')", self.script)
        self.assertIn("legacyDensity === 'compact' ? 3 : 0", self.script)
        self.assertIn('capture: captureGalleryPosition', self.script)
        self.assertNotIn("async function openTimelinePeriod(period) {\n  activePeriod = period;\n  galleryDensityLevel = 1;", self.script)
        self.assertIn('photo.detail', self.script)
        self.assertIn('photo.full', self.script)
        self.assertIn("viewerStage.addEventListener('wheel'", self.script)
        self.assertIn("gallery.addEventListener('pointermove'", self.script)
        self.assertIn("viewerStage.addEventListener('pointermove'", self.script)
        self.assertIn("viewerTouchPointers.size === 2", self.script)
        self.assertIn("setPointerCapture", self.script)
        self.assertIn('/static/gallery-density-gesture.js?v=6', self.template)
        self.assertIn('/static/gallery-window.js?v=5', self.template)
        self.assertLess(
            self.template.index('/static/gallery-density-gesture.js?v=6'),
            self.template.index('/static/gallery-window.js?v=5'),
        )
        self.assertLess(
            self.template.index('/static/gallery-window.js?v=5'),
            self.template.index('/static/gallery.js?v=62'),
        )
        self.assertIn('touch-action: pan-y;', self.media_styles)
        self.assertNotIn('touch-action: pan-x pan-y;', self.media_styles)
        self.assertIn('captureGalleryPointers(result.captureIds)', self.script)
        self.assertIn("event.stopImmediatePropagation()", self.script)
        self.assertNotIn('galleryTouchPointers', self.script)
        self.assertIn('suppressMilliseconds', self.gesture_script)
        self.assertIn('cancelAll', self.gesture_script)
        self.assertIn("gallery.addEventListener('touchstart'", self.script)
        self.assertIn("gallery.addEventListener('touchmove'", self.script)
        self.assertIn("{passive:false}", self.script)
        self.assertIn('event.touches.length===2', self.script)
        self.assertIn('first.accepted&&second.accepted', self.script)
        touch_end = self.script.split("gallery.addEventListener('touchend'", 1)[1].split("gallery.addEventListener('touchcancel'", 1)[0]
        self.assertNotIn('preventDefault', touch_end)

    def test_density_steps_are_exact_and_match_the_visible_across_label(self):
        self.assertIn('return galleryDensityApi.modeForLevel(galleryDensityLevel).columns', self.script)
        expected = {0: 3, 1: 6, 2: 9, 3: 13}
        for level, columns in expected.items():
            rule = (
                f'.gallery[data-density-level="{level}"] '
                f'{{ grid-template-columns: repeat({columns}, minmax(0, 1fr));'
            )
            self.assertEqual(self.media_styles.count(rule), 1, rule)
        self.assertIn('gridDensityLabel.value = `${mode.columns} across`', self.script)
        self.assertNotIn("window.matchMedia('(max-width: 760px)')", self.script)
        for level, size in ((2, 18), (3, 14)):
            selector = f'.gallery[data-density-level="{level}"] .photo-favorite'
            self.assertIn(selector, self.media_styles)
            density_block = self.media_styles.split(selector, 1)[1].split("}", 1)[0]
            self.assertIn(f"width: {size}px", density_block)
            self.assertIn(f"height: {size}px", density_block)
        self.assertIn('.gallery[data-density-level="3"] .photo-check', self.media_styles)

    def test_dense_media_is_browse_only_and_three_across_keeps_native_activation(self):
        self.assertIn('id="gridDensityMode">Tap to open or select</small>', self.template)
        self.assertIn('aria-describedby="gridDensityMode"', self.template)
        self.assertIn("gallery.dataset.interactionMode = mode.mode", self.script)
        self.assertIn("galleryDensityApi.applyCardMode(button, photo.original_name", self.script)
        self.assertIn("if (!galleryDensityApi.modeForLevel(galleryDensityLevel).interactive)", self.script)
        self.assertIn("gallery.addEventListener('click', (event) => {", self.script)
        photo_card = self.script[self.script.index('function photoCard('):self.script.index('function loadPhotos', self.script.index('function photoCard('))]
        self.assertNotIn("addEventListener('click'", photo_card)
        self.assertNotIn("addEventListener('error'", photo_card)
        self.assertIn('.gallery[data-interaction-mode="browse"] .photo {', self.media_styles)
        self.assertIn('pointer-events: none;', self.media_styles)
        self.assertIn('.gallery[data-interaction-mode="browse"] :is(.photo-favorite, .video-badge, .photo-check)', self.media_styles)
        self.assertIn('display: none;', self.media_styles)

    def test_selection_forces_interactive_density_and_locks_browse_only_steps(self):
        self.assertIn('function enterSelectionMode()', self.script)
        self.assertIn('if (switchedToInteractive) setGalleryDensity(0)', self.script)
        self.assertIn("if (selectionMode && nextLevel !== 0)", self.script)
        self.assertIn("gridZoomIn.disabled = galleryDensityLevel === 3 || selectionMode", self.script)
        self.assertIn('Switched to 3 across so media can be selected.', self.script)

    def test_density_preserves_anchor_and_cancels_stale_restore_on_refresh(self):
        self.assertIn('galleryDensityApi.createAnchorPreserver({', self.script)
        self.assertIn('restore: restoreGalleryPositionNow', self.script)
        self.assertIn('galleryDensityAnchor.mutate(change)', self.script)
        self.assertIn('galleryDensityAnchor.cancel()', self.script)

    def test_dense_grid_auto_paging_requires_fresh_user_scroll_intent(self):
        self.assertIn('galleryDensityApi.createIntersectionPageGate()', self.script)
        self.assertIn("{rootMargin: '0px 0px 64px 0px'}", self.script)
        self.assertNotIn("{rootMargin: '700px 0px'}", self.script)
        self.assertNotIn('window.innerHeight + 1000', self.script)
        self.assertIn('galleryAutoPageGate.update(', self.script)
        self.assertIn('galleryAutoPageGate.reset()', self.script)
        self.assertIn('galleryAutoPageGate.revokeIntent()', self.script)
        self.assertIn('galleryAutoPageGate.grantIntent()', self.script)
        self.assertIn("if (!galleryUsesIntersectionObserver && galleryAutoPageGate.hasIntent())", self.script)
        self.assertNotIn("window.addEventListener('scroll', queueGalleryViewportCheck", self.script)
        self.assertIn("window.addEventListener('wheel'", self.script)
        self.assertIn("window.addEventListener('touchmove'", self.script)
        self.assertIn('galleryTouchScroll.granted = true', self.script)

    def test_running_window_has_bounded_rows_releases_images_and_keeps_native_buttons(self):
        self.assertIn('const DEFAULT_CARD_CAPS = Object.freeze({3: 72, 6: 180, 9: 324, 13: 598})', self.window_script)
        self.assertIn('DEFAULT_OVERSCAN_BEFORE = 4', self.window_script)
        self.assertIn('DEFAULT_OVERSCAN_AFTER = 8', self.window_script)
        self.assertIn("type: 'month'", self.window_script)
        self.assertIn('flushPhotos();', self.window_script)
        self.assertIn('galleryTopSpacer.style.height', self.script)
        self.assertIn('galleryBottomSpacer.style.height', self.script)
        self.assertIn("window.addEventListener('scroll', () => {", self.script)
        self.assertIn('queueGalleryWindowRender()', self.script)
        self.assertIn("image.removeAttribute('src')", self.script)
        self.assertIn('node.replaceChildren()', self.script)
        self.assertIn("button.type = 'button'", self.script)
        self.assertNotIn("gallery.setAttribute('role', 'grid')", self.script)
        self.assertIn('if (!mode.interactive) return button', self.script)
        self.assertIn('galleryWindowManager.model().photoLocations.get', self.script)
        self.assertIn('galleryWindowApi.applySelectionState(button, selectedIds.has(photo.id), true)', self.script)
        self.assertIn("card.classList.toggle('chosen', active)", self.window_script)
        self.assertIn("card.setAttribute('aria-pressed', active ? 'true' : 'false')", self.window_script)
        self.assertIn('presentationSignatures.get(index) === signature', self.window_script)
        self.assertIn('if (!reconcileRequired && sameRange(range, lastRange))', self.window_script)
        self.assertIn('height: 62px;', self.media_styles)
        self.assertIn('line-height: 32px;', self.media_styles)
        self.assertIn('minimumPhotoRowHeight(viewportHeight, columns, cardCap)', self.window_script)
        self.assertIn("throw new RangeError('viewport exceeds the row model card cap", self.window_script)
        self.assertIn("node.style.setProperty('--gallery-row-card-height', `${row.cardHeight}px`)", self.script)
        self.assertIn('grid-auto-rows: var(--gallery-row-card-height);', self.media_styles)
        self.assertIn('aspect-ratio: auto;', self.media_styles)

    def test_metadata_append_preserves_virtual_scroll_anchor(self):
        self.assertIn('if (setOptions.preserveRows && mounted.size)', self.window_script)
        self.assertIn('options.setSpacers(lastRange.top, lastRange.bottom, lastRange)', self.window_script)
        self.assertNotIn('const cardCap = Math.max(\n      photoCount(model, visibleStart, visibleEnd)', self.window_script)
        load = self.script[self.script.index('async function performPhotoLoad'):self.script.index('async function refreshPhotos')]
        self.assertIn('const appendPosition = loadedPhotos.length ? captureGalleryPosition() : null', load)
        self.assertIn('rebuildGalleryWindow({preserveRows: true, position: appendPosition, append: true})', load)
        rebuild = self.script[self.script.index('function rebuildGalleryWindow'):self.script.index('function renderGalleryWindowNow')]
        self.assertIn('if (position) restoreGalleryPositionNow(position)', rebuild)

    def test_gallery_origin_is_coalesced_after_above_gallery_layout_changes(self):
        self.assertIn('createFrameScheduler(resyncGalleryWindowOrigin)', self.script)
        self.assertIn('function queueGalleryOriginResync()', self.script)
        self.assertIn('galleryDocumentTop = bounds.top + window.scrollY', self.script)
        self.assertIn("mediaTimeline.addEventListener('toggle', () => {\n  queueGalleryOriginResync();", self.script)
        summary = self.script[self.script.index('function updateGallerySummary()'):self.script.index('function configureEmptyState()')]
        self.assertIn('queueGalleryOriginResync()', summary)
        resync = self.script[self.script.index('function resyncGalleryWindowOrigin()'):self.script.index('function mountGalleryWindowRow')]
        self.assertNotIn('loadPhotos(', resync)
        self.assertNotIn('grantGalleryAutoPageIntent(', resync)

    def test_windowed_metadata_fetches_are_bounded_deduplicated_and_stale_safe(self):
        self.assertIn('MAX_METADATA_BATCH = 200', self.window_script)
        self.assertIn('galleryWindowApi.metadataBatchSize({', self.script)
        self.assertIn('galleryWindowApi.createGenerationRequestGate(galleryGeneration)', self.script)
        self.assertIn('return galleryMetadataRequests.run(generation', self.script)
        self.assertIn('if (generation !== galleryGeneration) return false', self.script)
        self.assertIn('galleryWindowApi.uniqueMetadataItems(result.photos, loadedPhotoIds)', self.script)
        self.assertIn('galleryWindowApi.isSafeNextCursor(', self.script)
        self.assertIn("error.code = 'gallery_cursor_repeated'", self.script)

    def test_complete_collections_page_merges_only_authorized_api_results(self):
        template = (ROOT / 'templates' / 'photo_collections.html').read_text(encoding='utf-8')
        script = (ROOT / 'static' / 'photo-collections.js').read_text(encoding='utf-8')
        self.assertIn('id="collectionsPageGrid"', template)
        self.assertIn("collectionApi('/api/collections')", script)
        self.assertIn("collectionApi('/api/collections?view=mine')", script)
        self.assertIn('encodeURIComponent(collection.id)', script)
        self.assertNotIn('innerHTML', script)


if __name__ == "__main__":
    unittest.main()
