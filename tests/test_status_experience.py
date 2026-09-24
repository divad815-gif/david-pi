from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_history_has_named_chart_summary_table_and_retry():
    template = (ROOT / "templates" / "status.html").read_text(encoding="utf-8")
    for marker in (
        'aria-labelledby="historyChartTitle historyChartDescription"',
        'id="historySummary" role="status"',
        'id="historyRows"',
        'id="historyCaption"',
        'id="historyRetry"',
    ):
        assert marker in template


def test_history_script_bounds_table_and_cancels_stale_requests():
    script = (ROOT / "static" / "status.js").read_text(encoding="utf-8")
    assert "function boundedHistoryRows(points, maximum = 48)" in script
    assert "historyController?.abort()" in script
    assert "generation !== historyGeneration" in script
    assert "svgElement('title'" in script
    assert "svgElement('desc'" in script
    assert "History could not be loaded. The rest of the status page is still available." in script


def test_history_metrics_define_human_labels_and_units():
    script = (ROOT / "static" / "status.js").read_text(encoding="utf-8")
    for label in ("Temperature", "One-minute load", "RAM use", "External drive use", "Health response", "Backup state"):
        assert f"label:'{label}'" in script
