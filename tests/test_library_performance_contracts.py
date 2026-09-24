from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_files_are_paged_filtered_in_sql_and_rendered_in_a_fragment():
    server = source("modules/files.py")
    client = source("static/files.js")
    assert "conditions.append(f\"({file_kind_sql()}) = ?\")" in server
    assert 'limit + 1, offset' in server
    assert 'ORDER BY updated_at DESC, id DESC' in server
    assert 'const FILE_PAGE_SIZE = 40' in client
    assert "summary:append?'0':'1'" in client
    assert 'document.createDocumentFragment()' in client


def test_recipe_followup_pages_skip_summary_work_and_use_smaller_batches():
    server = source("modules/recipes.py")
    client = source("static/recipes.js")
    assert 'include_summary = request.args.get("summary", "1") != "0"' in server
    assert 'if include_summary else None' in server
    assert 'const recipePageSize = 30' in client
    assert 'summary:append?0:1' in client
    assert 'requestIdleCallback' in client


def test_media_starts_visible_content_before_secondary_navigation_data():
    script = source("static/gallery.js")
    initial_start = script.index("async function loadInitialGallery()")
    initial_end = script.index("retryGallery.addEventListener", initial_start)
    initial = script[initial_start:initial_end]
    assert initial.index("loadCollections()") < initial.index("await refreshPhotos()")
    assert "loadTimeline(" not in initial
    assert "activeCollection !== requestedCollection" in initial
    assert "mediaTimeline.addEventListener('toggle'" in script


def test_pdf_fit_view_does_not_prefetch_zoom_renditions():
    client = source("static/files.js")
    assert 'fitRenderWidth=1200,zoomRenderWidth=2000' in client
    prefetch_start = client.index('function prefetch(page)')
    prefetch_end = client.index('function upgradeCurrentPage', prefetch_start)
    assert 'fitRenderWidth' in client[prefetch_start:prefetch_end]
    assert 'zoomRenderWidth' not in client[prefetch_start:prefetch_end]
