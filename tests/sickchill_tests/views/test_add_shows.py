import json
from unittest import mock

import sickchill
from sickchill import settings
from sickchill.views.manage.add_shows import AddShows


def test_search_all_indexers_uses_default_indexer_language():
    handler = AddShows.__new__(AddShows)
    arguments = {"search_term": "Test Show", "lang": "en", "indexer": "0", "exact": "0"}
    handler.set_header = mock.Mock()
    handler.get_body_argument = lambda name, default=None: arguments.get(name, default)

    search_result = {"id": 123, "seriesName": "Test Show", "firstAired": "2024-01-01"}
    tvdb = sickchill.indexer[sickchill.indexer.TVDB]

    with mock.patch.object(tvdb, "search", return_value=[search_result]):
        response = json.loads(handler.searchIndexersForShowName())

    assert response["success"] is True
    assert response["langid"] == tvdb.lang_dict["en"]
    assert response["results"][0][1] == sickchill.indexer.TVDB
