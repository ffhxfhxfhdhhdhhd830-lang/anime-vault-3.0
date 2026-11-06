from scripts.import_workbook import STATUS_MAP


def test_importer_maps_all_watchlist_statuses():
    assert STATUS_MAP["planning"] == "plan"
    assert STATUS_MAP["on hold"] == "on_hold"
    assert STATUS_MAP["rewatching"] == "rewatching"
    assert STATUS_MAP["not interested"] == "not_interested"
    assert STATUS_MAP["completed"] == "completed"
