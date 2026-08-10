from types import SimpleNamespace
from unittest import mock

import requests

from sickchill.show.indexers.tvdb import _TVDBUpdates, _TVDBV4Client, TVDB


class FakeResponse(object):
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

    def json(self):
        return self.payload


def test_v4_client_authenticates_and_retries_an_expired_token():
    session = mock.Mock()
    session.request.side_effect = [
        FakeResponse(200, {"status": "success", "data": {"token": "expired"}}),
        FakeResponse(401, {"status": "failure"}),
        FakeResponse(200, {"status": "success", "data": {"token": "fresh"}}),
        FakeResponse(200, {"status": "success", "data": {"id": 123}}),
    ]

    client = _TVDBV4Client("project-key", "subscriber-pin", session=session)
    data, links = client.request("series/123")

    assert data == {"id": 123}
    assert links == {}
    assert session.request.call_args_list[0].kwargs["json"] == {"apikey": "project-key", "pin": "subscriber-pin"}
    assert session.request.call_args_list[-1].kwargs["headers"] == {"Authorization": "Bearer fresh"}


def test_v4_series_data_is_adapted_to_sickchill_fields():
    tvdb = TVDB()
    series = tvdb._series_result(
        {
            "id": 123,
            "name": "Original Name",
            "firstAired": "2024-01-02",
            "averageRuntime": 42,
            "status": {"name": "Continuing"},
            "latestNetwork": {"name": "Test Network"},
            "genres": [{"name": "Drama"}],
            "remoteIds": [{"sourceName": "IMDB", "id": "tt1234567"}],
            "airsDays": {"monday": True, "tuesday": False},
            "airsTime": "20:00",
            "translations": {
                "nameTranslations": [{"language": "fra", "name": "Nom traduit"}],
                "overviewTranslations": [{"language": "fra", "overview": "Résumé"}],
            },
        },
        "fr",
    )

    assert series.id == 123
    assert series.seriesName == "Nom traduit"
    assert series.overview == "Résumé"
    assert series.network == "Test Network"
    assert series.genre == ["Drama"]
    assert series.imdbId == "tt1234567"
    assert series.airsDayOfWeek == "Monday"


def test_v4_episode_lookup_uses_series_and_extended_endpoints():
    tvdb = TVDB()
    client = mock.Mock()
    client.request.side_effect = [
        ({"episodes": [{"id": 456, "seasonNumber": 2, "number": 3}]}, {}),
        (
            {
                "id": 456,
                "seriesId": 123,
                "seasonNumber": 2,
                "number": 3,
                "name": "Episode title",
                "aired": "2024-02-03",
                "image": "banners/episode.jpg",
                "characters": [{"type": 1, "personName": "Director Name"}],
            },
            {},
        ),
    ]
    show = SimpleNamespace(indexerid=123, dvdorder=False, lang="en")

    with mock.patch.object(tvdb, "_get_client", return_value=client):
        result = tvdb.episode(show, 2, 3)

    assert result["episodeName"] == "Episode title"
    assert result["airedSeason"] == 2
    assert result["airedEpisodeNumber"] == 3
    assert result["directors"] == ["Director Name"]
    assert client.request.call_args_list[0].args[0] == "series/123/episodes/default/eng"
    assert client.request.call_args_list[1].args[0] == "episodes/456/extended"


def test_v4_updates_expose_legacy_series_ids():
    client = mock.Mock()
    client.request.return_value = ([{"recordId": 123, "timeStamp": 20}], {})
    updates = _TVDBUpdates(client, 10, 30)

    assert updates.series() == [{"id": 123, "recordId": 123, "timeStamp": 20}]
    assert updates.series == [{"id": 123, "recordId": 123, "timeStamp": 20}]
