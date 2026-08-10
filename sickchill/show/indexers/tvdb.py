import html
import json
import re
import threading
import traceback

import requests

import sickchill.start
from sickchill import logger, settings
from sickchill.tv import TVEpisode

from .base import Indexer
from .wrappers import ExceptionDecorator


class _TVDBSeries(dict):
    def __init__(self, data, language=None):
        super(_TVDBSeries, self).__init__(data)
        self.language = language

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def info(self, language=None):
        if language:
            self.language = language
        return self


class _TVDBV4Client(object):
    def __init__(self, api_key, pin=None, timeout=20, session=None):
        self.api_key = api_key
        self.pin = pin or None
        self.timeout = timeout
        self.base_url = "https://api4.thetvdb.com/v4"
        self.session = session or requests.Session()
        self.token = None
        self._auth_lock = threading.Lock()

    def _authenticate(self):
        if not self.api_key:
            raise ValueError("A TheTVDB v4 project API key is required")

        with self._auth_lock:
            if self.token:
                return

            login_data = {"apikey": self.api_key}
            if self.pin:
                login_data["pin"] = self.pin

            response = self.session.request(
                "POST",
                f"{self.base_url}/login",
                json=login_data,
                timeout=self.timeout,
                verify=bool(settings.SSL_VERIFY),
            )
            response.raise_for_status()
            payload = response.json()
            self.token = payload.get("data", {}).get("token")
            if not self.token:
                raise requests.exceptions.HTTPError(payload.get("message", "TheTVDB v4 login did not return a bearer token"), response=response)

    def request(self, path, params=None, retry=True):
        self._authenticate()
        response = self.session.request(
            "GET",
            f'{self.base_url}/{path.lstrip("/")}',
            params=params,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout,
            verify=bool(settings.SSL_VERIFY),
        )

        if response.status_code == 401 and retry:
            self.token = None
            return self.request(path, params=params, retry=False)

        response.raise_for_status()
        payload = response.json()
        if payload.get("status") == "failure":
            raise requests.exceptions.HTTPError(payload.get("message", "TheTVDB v4 request failed"), response=response)

        return payload.get("data"), payload.get("links") or {}


class _TVDBUpdates(object):
    def __init__(self, client, from_time, to_time=None):
        self.client = client
        self.from_time = from_time
        self.to_time = to_time

    def series(self):
        page = 0
        updates = []
        while page <= 100:
            data, links = self.client.request("updates", params={"since": self.from_time, "type": "series", "page": page})
            for update in data or []:
                timestamp = update.get("timeStamp")
                if self.to_time is None or timestamp is None or timestamp <= self.to_time:
                    updates.append({"id": update.get("recordId"), **update})

            if not links.get("next"):
                break
            page += 1

        self.series = updates
        return updates


class TVDB(Indexer):
    language_codes = {
        "cs": "ces",
        "da": "dan",
        "de": "deu",
        "el": "ell",
        "en": "eng",
        "es": "spa",
        "fi": "fin",
        "fr": "fra",
        "he": "heb",
        "hr": "hrv",
        "hu": "hun",
        "it": "ita",
        "ja": "jpn",
        "ko": "kor",
        "nl": "nld",
        "no": "nor",
        "pl": "pol",
        "pt": "por",
        "ru": "rus",
        "sl": "slv",
        "sv": "swe",
        "tr": "tur",
        "zh": "zho",
    }
    artwork_types = {"series": 1, "poster": 2, "fanart": 3, "seasonwide": 6, "season": 7}

    def __init__(self):
        super(TVDB, self).__init__()
        self.name = "theTVDB"
        self.slug = "tvdb"
        self.api_key = settings.TVDB_API_KEY
        self.show_url = "https://thetvdb.com/series/"
        self.base_url = "https://api4.thetvdb.com/v4/"
        self.icon = "images/indexers/thetvdb16.png"
        self._client = None
        self._client_credentials = None

    def _get_client(self, pin=None):
        api_key = settings.TVDB_API_KEY or self.api_key
        subscriber_pin = settings.TVDB_USER_KEY if pin is None else pin
        credentials = api_key, subscriber_pin
        if self._client is None or self._client_credentials != credentials:
            from sickchill.oldbeard import helpers

            self.api_key = api_key
            self._client = _TVDBV4Client(api_key, subscriber_pin, self.timeout, helpers.make_indexer_session())
            self._client_credentials = credentials
        return self._client

    def _search_endpoint(self):
        return f"{self.base_url}search"

    def _log_search_error(self, search_term, language, error):
        request = getattr(error, "request", None)
        request_url = getattr(request, "url", None)
        request_details = f" Request URL: {request_url}" if request_url else f" Search endpoint: {self._search_endpoint()}"
        logger.warning(f'theTVDB API v4 search failed for "{search_term}" in language "{language}": {error}.{request_details}')
        logger.debug(traceback.format_exc())

    @classmethod
    def _language_code(cls, language):
        return cls.language_codes.get(language, language or "eng")

    @staticmethod
    def _translation(data, language):
        translations = data.get("translations") or {}
        if not isinstance(translations, dict):
            return {}
        result = {}
        for key, field in (("nameTranslations", "name"), ("overviewTranslations", "overview")):
            for translation in translations.get(key) or []:
                if translation.get("language") == language and translation.get(field):
                    result[field] = translation[field]
                    break
        return result

    @staticmethod
    def _remote_id(data, source):
        for remote_id in data.get("remoteIds") or data.get("remote_ids") or []:
            if source in (remote_id.get("sourceName") or "").lower():
                return remote_id.get("id") or ""
        return ""

    @staticmethod
    def _network(data):
        for key in ("latestNetwork", "originalNetwork"):
            if isinstance(data.get(key), dict) and data[key].get("name"):
                return data[key]["name"]
        for company in data.get("companies") or []:
            if isinstance(company, dict) and company.get("name"):
                return company["name"]
            if isinstance(company, str):
                return company
        return data.get("network") or ""

    @staticmethod
    def _actors(data):
        actors = []
        for character in data.get("characters") or []:
            people_type = character.get("peopleType") or ""
            if character.get("type") not in (3, 4) and people_type not in ("Actor", "Guest Star"):
                continue
            actor_name = character.get("personName") or ""
            if actor_name:
                actors.append({"name": actor_name, "role": character.get("name") or "", "image": character.get("personImgURL") or character.get("image") or ""})
        return actors

    def _series_result(self, data, language=None):
        language_code = self._language_code(language)
        translation = self._translation(data, language_code)
        status = data.get("status") or "Unknown"
        if isinstance(status, dict):
            status = status.get("name") or "Unknown"

        airs_days = data.get("airsDays") or {}
        air_days = [day.title() for day, enabled in airs_days.items() if enabled]
        genres = [genre.get("name") if isinstance(genre, dict) else genre for genre in data.get("genres") or []]
        raw_id = data.get("tvdb_id") or data.get("id")
        id_match = re.search(r"(\d+)$", str(raw_id or ""))
        if not id_match:
            raise ValueError("TheTVDB v4 result did not contain a series id")
        result = {
            "id": int(id_match.group(1)),
            "seriesName": translation.get("name") or data.get("name_translated") or data.get("name") or data.get("title") or "",
            "firstAired": data.get("firstAired") or data.get("first_air_time") or (f'{data.get("year")}-01-01' if data.get("year") else ""),
            "overview": translation.get("overview") or data.get("overview_translated") or data.get("overview") or "",
            "network": self._network(data),
            "status": status,
            "genre": genres or data.get("genres") or [],
            "runtime": str(data.get("averageRuntime") or ""),
            "imdbId": self._remote_id(data, "imdb"),
            "airsDayOfWeek": ", ".join(air_days),
            "airsTime": data.get("airsTime") or "",
            "slug": data.get("slug") or "",
            "banner": data.get("image") or data.get("image_url") or "",
            "siteRating": None,
            "actors": self._actors(data),
        }
        return _TVDBSeries(result, language)

    def _fetch_series(self, indexer_id, language=None):
        data, _ = self._get_client().request(f"series/{int(indexer_id)}/extended", params={"meta": "translations"})
        return self._series_result(data, language)

    @ExceptionDecorator()
    def series(self, *args, **kwargs):
        indexer_id = kwargs.pop("id", None) or (args[0] if args else None)
        language = kwargs.get("language") or (args[1] if len(args) > 1 else None)
        return self._fetch_series(indexer_id, language)

    @ExceptionDecorator()
    def get_series_by_id(self, indexerid, language=None):
        return self._fetch_series(indexerid, language)

    @ExceptionDecorator()
    def series_from_show(self, show):
        return self._fetch_series(show.indexerid, show.lang)

    def series_from_episode(self, episode):
        return self.series_from_show(episode.show)

    def get_series_by_name(self, name, indexerid=None, language=None):
        if indexerid:
            return self.get_series_by_id(indexerid, language)
        try:
            if isinstance(name, (list, tuple)):
                name = name[0]
            result = self.search(name, language)[0]
            return self._fetch_series(result["id"], language)
        except (IndexError, KeyError, TypeError):
            return None

    @staticmethod
    def _episode_result(data, season_type="default"):
        season = data.get("seasonNumber")
        episode = data.get("number")
        characters = data.get("characters") or []
        directors = [
            item.get("personName") for item in characters if item.get("personName") and (item.get("type") == 1 or item.get("peopleType") == "Director")
        ]
        writers = [item.get("personName") for item in characters if item.get("personName") and (item.get("type") == 2 or item.get("peopleType") == "Writer")]
        guest_stars = [
            item.get("personName") for item in characters if item.get("personName") and (item.get("type") == 4 or item.get("peopleType") == "Guest Star")
        ]
        imdb_id = TVDB._remote_id(data, "imdb")
        return {
            "id": data.get("id"),
            "seriesId": data.get("seriesId"),
            "episodeName": data.get("name") or "",
            "overview": data.get("overview") or "",
            "firstAired": data.get("aired") or "",
            "airedSeason": season,
            "airedEpisodeNumber": episode,
            "dvdSeason": season if season_type == "dvd" else None,
            "dvdEpisodeNumber": episode if season_type == "dvd" else None,
            "absoluteNumber": data.get("absoluteNumber"),
            "filename": data.get("image") or "",
            "runtime": data.get("runtime"),
            "imdbId": imdb_id,
            "siteRating": None,
            "rating": None,
            "directors": directors,
            "writers": writers,
            "guestStars": guest_stars,
            "language": {"overview": data.get("overview") or ""},
        }

    def _episode_page(self, show, season_type, season=None, episode=None, language=None, air_date=None, page=0):
        params = {"page": page}
        if season is not None:
            params["season"] = season
        if episode is not None:
            params["episodeNumber"] = episode
        if air_date is not None:
            params["airDate"] = str(air_date)
        path = f"series/{show.indexerid}/episodes/{season_type}"
        if language:
            path += f"/{self._language_code(language)}"
        try:
            return self._get_client().request(path, params=params)
        except requests.exceptions.HTTPError:
            if not language or self._language_code(language) == "eng":
                raise
            path = f"series/{show.indexerid}/episodes/{season_type}"
            return self._get_client().request(path, params=params)

    @ExceptionDecorator()
    def episodes(self, show, season=None):
        season_type = "dvd" if show.dvdorder else "default"
        page = 0
        episodes = []
        while page <= 100:
            data, links = self._episode_page(show, season_type, season=season, language=show.lang, page=page)
            episodes.extend(self._episode_result(item, season_type) for item in (data or {}).get("episodes") or [])
            if not links.get("next"):
                break
            page += 1
        return episodes

    @ExceptionDecorator()
    def episode(self, item, season=None, episode=None, **kwargs):
        if isinstance(item, TVEpisode):
            show = item.show
            season = item.season
            episode = item.episode
        else:
            show = item

        season_type = "dvd" if show.dvdorder else "default"
        data, _ = self._episode_page(show, season_type, season=season, episode=episode, language=show.lang, air_date=kwargs.get("firstAired"))
        results = (data or {}).get("episodes") or []
        if not results:
            return None
        extended, _ = self._get_client().request(f'episodes/{results[0]["id"]}/extended', params={"meta": "translations"})
        episode_data = dict(extended or results[0])
        for key in ("name", "overview"):
            if results[0].get(key):
                episode_data[key] = results[0][key]
        return self._episode_result(episode_data, season_type)

    @ExceptionDecorator(default_return=list())
    def search(self, name, language=None, exact=False, indexer_id=False):
        language = language or self.language
        if isinstance(name, bytes):
            name = name.decode()
        if isinstance(name, (list, tuple)):
            name = name[0] if name else ""
        if not name:
            return []

        if re.match(r"^\d{5,8}$", name):
            series = self.get_series_by_id(name, language)
            return [series] if series else []

        names = [name]
        if not exact:
            match = re.match(r"^(.+?)[. -]+\(\d{4}\)?$", name)
            if match:
                names.append(match.group(1).strip())
            if re.search(r"[. _-]", name):
                names.append(re.sub(r"[. _-]+", " ", name).strip())

        remote_id = None
        if re.match(r"^t?t?\d{7,8}$", name):
            remote_id = f'tt{name.strip("t")}'
            names = [name]

        for attempt in dict.fromkeys(item for item in names if item.strip()):
            try:
                params = {"query": attempt, "type": "series", "meta": "translations", "language": self._language_code(language)}
                if remote_id:
                    params["remote_id"] = remote_id
                data, _ = self._get_client().request("search", params=params)
                results = [self._series_result(item, language) for item in data or [] if item.get("type") in (None, "series")]
                if results:
                    return results
            except Exception as error:
                self._log_search_error(attempt, language, error)
        return []

    @property
    def languages(self):
        return list(self.language_codes)

    @property
    def lang_dict(self):
        return {
            "el": 20,
            "en": 7,
            "zh": 27,
            "it": 15,
            "cs": 28,
            "es": 16,
            "ru": 22,
            "nl": 13,
            "pt": 26,
            "no": 9,
            "tr": 21,
            "pl": 18,
            "fr": 17,
            "hr": 31,
            "de": 14,
            "da": 10,
            "fi": 11,
            "hu": 19,
            "ja": 25,
            "he": 24,
            "ko": 32,
            "sv": 8,
            "sl": 30,
        }

    @staticmethod
    def complete_image_url(location):
        location = (location or "").strip()
        if not location:
            return location
        if location.startswith(("http://", "https://")):
            return location
        return f'https://artworks.thetvdb.com/{re.sub(r"^_cache/", "", location).lstrip("/")}'

    @staticmethod
    @ExceptionDecorator()
    def actors(series):
        return getattr(series, "actors", [])

    @ExceptionDecorator(default_return="", catch=(requests.exceptions.RequestException, KeyError, TypeError, ValueError), image_api=True)
    def __call_images_api(self, show, thumb, key_type, season=None, lang=None, multiple=False):
        data, _ = self._get_client().request(f"series/{show.indexerid}/extended")
        artworks = data.get("artworks") or []
        if season is not None:
            season_type = "dvd" if show.dvdorder else "official"
            season_data = next(
                (item for item in data.get("seasons") or [] if item.get("number") == season and (item.get("type") or {}).get("type") == season_type),
                None,
            )
            if not season_data:
                return [] if multiple else ""
            season_extended, _ = self._get_client().request(f'seasons/{season_data["id"]}/extended')
            artworks = season_extended.get("artwork") or []

        type_id = self.artwork_types[key_type]
        language = self._language_code(lang or show.lang)
        matching = [artwork for artwork in artworks if artwork.get("type") == type_id and artwork.get("language") in (language, None, "")]
        if not matching and language != "eng":
            matching = [artwork for artwork in artworks if artwork.get("type") == type_id and artwork.get("language") in ("eng", None, "")]
        matching.sort(key=lambda artwork: artwork.get("score") or 0, reverse=True)
        urls = [self.complete_image_url(artwork.get("thumbnail") if thumb and artwork.get("thumbnail") else artwork.get("image")) for artwork in matching]
        return urls if multiple else (urls[0] if urls else "")

    def series_poster_url(self, show, thumb=False, multiple=False):
        return self.__call_images_api(show, thumb, "poster", multiple=multiple)

    def series_banner_url(self, show, thumb=False, multiple=False):
        return self.__call_images_api(show, thumb, "series", multiple=multiple)

    def series_fanart_url(self, show, thumb=False, multiple=False):
        return self.__call_images_api(show, thumb, "fanart", multiple=multiple)

    def season_poster_url(self, show, season, thumb=False, multiple=False):
        return self.__call_images_api(show, thumb, "season", season, multiple=multiple)

    def season_banner_url(self, show, season, thumb=False, multiple=False):
        return self.__call_images_api(show, thumb, "seasonwide", season, multiple=multiple)

    @ExceptionDecorator(default_return="", catch=(requests.exceptions.RequestException, KeyError, TypeError))
    def episode_image_url(self, episode):
        return self.complete_image_url(self.episode(episode)["filename"])

    def episode_guide_url(self, show):
        login_data = {"apikey": settings.TVDB_API_KEY or self.api_key, "id": show.indexerid}
        if settings.TVDB_USER_KEY:
            login_data["pin"] = settings.TVDB_USER_KEY
        data = html.escape(json.dumps(login_data)).replace(" ", "")
        return f"{self.base_url}login?{data}|Content-Type=application/json"

    def updates(self, fromTime, toTime=None):
        return _TVDBUpdates(self._get_client(), fromTime, toTime)

    def get_favorites(self):
        if not settings.TVDB_USER_KEY:
            return []

        data, _ = self._get_client().request("user/favorites")
        if isinstance(data, list):
            favorite_ids = [series_id for item in data for series_id in item.get("series", [])]
        else:
            favorite_ids = (data or {}).get("series", [])
        return [self.get_series_by_id(series_id) for series_id in favorite_ids]

    @staticmethod
    def test_user_key(user, key):
        try:
            client = _TVDBV4Client(settings.TVDB_API_KEY, key, settings.INDEXER_TIMEOUT)
            client.request("user")
        except Exception:
            logger.exception(traceback.format_exc())
            return False

        settings.TVDB_USER = user
        settings.TVDB_USER_KEY = key
        sickchill.start.save_config()
        return True
