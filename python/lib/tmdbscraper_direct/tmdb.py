from datetime import datetime, timedelta
from difflib import SequenceMatcher
import re
from urllib.parse import quote
from . import tmdbapi
from . import api_utils
from . import pinyin

import json

class TMDBMovieScraper(object):
    def __init__(self, url_settings, language, certification_country, search_language="", include_adult=False):
        self.url_settings = url_settings
        self.language = language
        self.certification_country = certification_country
        if(search_language == ""):
            self.search_language = language
        else:
            self.search_language = search_language
        self.include_adult = include_adult
        self._urls = None
        self._english_title_cache = {}

    @property
    def urls(self):
        if not self._urls:
            self._urls = _load_base_urls(self.url_settings)
        return self._urls

    def _get_image_proxy(self):
        try:
            if self.url_settings:
                proxy = self.url_settings.getSettingString('image_proxy_prefix')
            else:
                proxy = ""
            if not proxy:
                proxy = 'https://wsrv.nl/?url='
            return proxy
        except:
            return 'https://wsrv.nl/?url='

    def search(self, title, year=None):
        search_media_id = _parse_media_id(title)
        if search_media_id:
            if search_media_id['type'] == 'tmdb':
                result = _get_movie(search_media_id['id'], None, True)
                if 'error' in result:
                    return result
                result = [result]
            else:
                result = tmdbapi.find_movie_by_external_id(search_media_id['id'], language=self.search_language, settings=self.url_settings)
                if 'error' in result:
                    return result
                result = result.get('movie_results')
        else:
            response = tmdbapi.search_movie(query=title, year=year, language=self.search_language, settings=self.url_settings, include_adult=self.include_adult)
            if 'error' in response:
                return response
            result = response['results']
            # Get second page when first page has no strong lexical match.
            if response['total_pages'] > 1:
                bests = [
                    item
                    for item in result
                    if _is_confident_match(_score_search_match(item, title, year)) and item.get('popularity', 0) > 5
                ]
                if not bests:
                    response = tmdbapi.search_movie(query=title, year=year, language=self.language, page=2, settings=self.url_settings, include_adult=self.include_adult)
                    if not 'error' in response:
                        result += response['results']
        urls = self.urls

        if result:
            result, has_confident = self._sort_results_by_match(result, title, year)

            # When all candidates are low-confidence, enrich with aliases and
            # localized/English titles from details for a second lexical pass.
            if not has_confident:
                enrich_limit = 8 if _query_looks_english(title) else 3
                self._enrich_results_with_english_titles(result, limit=enrich_limit)
                result, has_confident = self._sort_results_by_match(result, title, year)

            # Avoid high-risk false positives in unattended scan for pure English queries.
            if not has_confident and _query_looks_english(title):
                return []

        proxy = self._get_image_proxy()

        for item in result:
            if item.get('poster_path'):
                item['poster_path'] = proxy + urls['preview'] + item['poster_path']
            if item.get('backdrop_path'):
                item['backdrop_path'] = proxy + urls['preview'] + item['backdrop_path']
        return result

    def _sort_results_by_match(self, items, search_title, search_year):
        scored = []
        for item in items:
            meta = _score_search_match(item, search_title, search_year)
            popularity = item.get('popularity', 0) or 0
            scored.append((meta['score'], popularity, item, meta))

        scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        sorted_items = [entry[2] for entry in scored]
        has_confident = any(_is_confident_match(entry[3]) for entry in scored)
        return sorted_items, has_confident

    def _enrich_results_with_english_titles(self, items, limit=8):
        for item in items[:limit]:
            movie_id = item.get('id')
            if not movie_id:
                continue

            cached = self._english_title_cache.get(movie_id)
            if cached is None:
                cached = {'title': '', 'original_title': '', 'aliases': []}
                try:
                    english_movie = tmdbapi.get_movie(
                        movie_id,
                        language='en-US',
                        append_to_response='alternative_titles',
                        settings=self.url_settings,
                    )
                    if isinstance(english_movie, dict) and 'error' not in english_movie:
                        cached['title'] = english_movie.get('title', '') or ''
                        cached['original_title'] = english_movie.get('original_title', '') or ''

                        aliases = []
                        alt_payload = english_movie.get('alternative_titles') or {}
                        alt_items = []
                        if isinstance(alt_payload, dict):
                            alt_items = alt_payload.get('titles') or alt_payload.get('results') or []
                        elif isinstance(alt_payload, list):
                            alt_items = alt_payload

                        for alt in alt_items:
                            if isinstance(alt, dict):
                                alias = (alt.get('title') or alt.get('name') or '').strip()
                            else:
                                alias = str(alt).strip()

                            if alias and alias not in aliases:
                                aliases.append(alias)

                        also_known_as = english_movie.get('also_known_as') or []
                        if isinstance(also_known_as, list):
                            for alias in also_known_as:
                                alias_text = str(alias).strip()
                                if alias_text and alias_text not in aliases:
                                    aliases.append(alias_text)

                        cached['aliases'] = aliases
                except Exception:
                    pass
                self._english_title_cache[movie_id] = cached

            if cached.get('title'):
                item['english_title'] = cached['title']
            if cached.get('original_title'):
                item['english_original_title'] = cached['original_title']
            if cached.get('aliases'):
                aliases = list(cached['aliases'])
                item['english_alias_titles'] = aliases
                merged_aliases = []
                existing_aliases = item.get('alias_titles') or []
                if not isinstance(existing_aliases, (list, tuple, set)):
                    existing_aliases = [existing_aliases]
                for alias in list(existing_aliases) + aliases:
                    alias_text = str(alias).strip()
                    if alias_text and alias_text not in merged_aliases:
                        merged_aliases.append(alias_text)
                if merged_aliases:
                    item['alias_titles'] = merged_aliases

    def get_details(self, uniqueids):
        media_id = uniqueids.get('tmdb')
        if not media_id:
            imdb_id = uniqueids.get('imdb')
            if not imdb_id:
                return None

            find_results = tmdbapi.find_movie_by_external_id(imdb_id, language=self.search_language, settings=self.url_settings)
            if 'error' in find_results:
                return find_results
            if find_results.get('movie_results'):
                movie = find_results['movie_results'][0]
                media_id = movie['id']
            if not media_id:
                return None

        details = self._gather_details(media_id)
        if not details:
            return None
        if details.get('error'):
            return details
        return self._assemble_details(**details)

    def _gather_details(self, media_id):

        
        details_lang = 'trailers,images,releases,casts,keywords'
        details_fallback = 'trailers,images'
        
        movie = _get_movie(media_id, self.language)
        movie_fallback = _get_movie(media_id)

        if not movie or movie.get('error'):
            return movie
            
        if not self.include_adult and movie.get('adult'):
            return {'error': 'Adult content is disabled'}

        movie['images'] = movie_fallback.get('images', {})

        # Handle Collections
        collection_id = movie.get('belongs_to_collection', {}).get('id') if movie.get('belongs_to_collection') else None
        
        collection = None
        collection_fallback = None
        
        if collection_id:
            # See _get_moviecollection helper
            collection = _get_moviecollection(collection_id, self.language)
            collection_fallback = _get_moviecollection(collection_id)

        if collection and collection_fallback and 'images' in collection_fallback:
            collection['images'] = collection_fallback['images']

        return {'movie': movie, 'movie_fallback': movie_fallback, 'collection': collection,
            'collection_fallback': collection_fallback}

    def _assemble_details(self, movie, movie_fallback, collection, collection_fallback):
        # Generate Pinyin Initials
        pinyin_initials = pinyin.get_pinyin_permutations(movie['title'])
        
        # Check setting
        write_initials = True
        write_initials_originaltitle = True
        if self.url_settings:
             write_initials = self.url_settings.getSettingBool('write_initials')
             write_initials_originaltitle = self.url_settings.getSettingBool('write_initials_originaltitle')

        # SortTitle: All pinyin combinations + Title
        sort_title = ""
        original_title = movie['original_title']

        if pinyin_initials:
            if write_initials:
                sort_title = "{}|{}".format(pinyin_initials, movie['title'])
            
            if write_initials_originaltitle:
                original_title = "{}|{}|{}".format(pinyin_initials, movie['title'], original_title)

        info = {
            'title': movie['title'],
            'originaltitle': original_title,
            'sorttitle': sort_title,
            'plot': movie.get('overview') or movie_fallback.get('overview'),
            'tagline': movie.get('tagline') or movie_fallback.get('tagline'),
            'studio': _get_names(movie['production_companies']),
            'genre': _get_names(movie['genres']),
            'country': _get_names(movie['production_countries']),
            'credits': _get_cast_members(movie['casts'], 'crew', 'Writing', ['Screenplay', 'Writer', 'Author'], self._get_image_proxy(), self.urls['original']),
            'director': _get_cast_members(movie['casts'], 'crew', 'Directing', ['Director'], self._get_image_proxy(), self.urls['original']),
            'premiered': movie['release_date'],
            'tag': _get_names(movie['keywords']['keywords'])
        }

        if 'countries' in movie['releases']:
            certcountry = self.certification_country.upper()
            for country in movie['releases']['countries']:
                if country['iso_3166_1'] == certcountry and country['certification']:
                    info['mpaa'] = country['certification']
                    break

        trailer = _fetch_maoyan_trailer(movie['title'], movie.get('release_date', ''))
        if trailer:
            info['trailer'] = trailer
        if collection:
            info['set'] = collection.get('name') or collection_fallback.get('name')
            info['setoverview'] = collection.get('overview') or collection_fallback.get('overview')
        if movie.get('runtime'):
            info['duration'] = movie['runtime'] * 60

        ratings = {'themoviedb': {'rating': float(movie['vote_average']), 'votes': int(movie['vote_count'])}}
        uniqueids = {'tmdb': str(movie['id']), 'imdb': movie['imdb_id']}
        cast = [{
                'name': actor['name'],
                'role': actor['character'],
                'thumbnail': self._get_image_proxy() + self.urls['original'] + actor['profile_path']
                    if actor['profile_path'] else "",
                'order': actor['order']
            }
            for actor in movie['casts'].get('cast', [])
        ]
        available_art = _parse_artwork(movie, collection, self.urls, self.language, self._get_image_proxy())

        _info = {'set_tmdbid': movie['belongs_to_collection'].get('id')
            if movie['belongs_to_collection'] else None}

        return {'info': info, 'ratings': ratings, 'uniqueids': uniqueids, 'cast': cast,
            'available_art': available_art, '_info': _info}

def _parse_media_id(title):
    if title.startswith('tt') and title[2:].isdigit():
        return {'type': 'imdb', 'id':title} # IMDB ID works alone because it is clear
    title = title.lower()
    if title.startswith('tmdb/') and title[5:].isdigit(): # TMDB ID
        return {'type': 'tmdb', 'id':title[5:]}
    elif title.startswith('imdb/tt') and title[7:].isdigit(): # IMDB ID with prefix to match
        return {'type': 'imdb', 'id':title[5:]}
    return None


def _query_looks_english(title):
    return bool(re.search(r'[a-zA-Z]', title or ''))


def _normalize_match_text(value):
    text = (value or '').lower()
    text = re.sub(r'[^\w\u4e00-\u9fff]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _iter_match_candidates(item):
    keys = (
        'title', 'original_title', 'name', 'original_name',
        'english_title', 'english_original_title',
        'title_en', 'original_title_en', 'english_name',
        'english_alias_titles', 'alias_titles', 'aliases', 'alternative_titles',
    )
    for key in keys:
        value = item.get(key, '')
        for entry in _iter_match_candidate_values(value):
            yield entry


def _iter_match_candidate_values(value):
    if isinstance(value, (list, tuple, set)):
        for entry in value:
            for nested in _iter_match_candidate_values(entry):
                yield nested
        return

    if isinstance(value, dict):
        direct_title = (
            value.get('title')
            or value.get('name')
            or value.get('original_title')
            or value.get('original_name')
            or ''
        )
        if direct_title:
            yield direct_title

        nested_candidates = value.get('titles') or value.get('results') or []
        for nested in _iter_match_candidate_values(nested_candidates):
            yield nested
        return

    if value:
        yield value


def _score_search_match(item, search_title, search_year):
    query = _normalize_match_text(search_title)
    if not query:
        return {
            'score': -999.0,
            'similarity': 0.0,
            'contains': False,
            'exact': False,
            'year_ok': False,
        }

    best_similarity = 0.0
    contains = False
    exact = False

    for candidate_raw in _iter_match_candidates(item):
        candidate = _normalize_match_text(candidate_raw)
        if not candidate:
            continue

        if candidate == query:
            exact = True

        if len(query) >= 3 and (query in candidate or candidate in query):
            contains = True

        similarity = SequenceMatcher(None, query, candidate).ratio()
        if similarity > best_similarity:
            best_similarity = similarity

    release_date = item.get('release_date', '') or item.get('first_air_date', '') or ''
    year_ok = (not search_year) or release_date.startswith(str(search_year))

    score = best_similarity * 100.0
    if exact:
        score += 50.0
    if contains:
        score += 35.0
    if year_ok:
        score += 20.0
    else:
        score -= 20.0

    try:
        score += min(float(item.get('popularity', 0.0)), 100.0) / 20.0
    except Exception:
        pass

    return {
        'score': score,
        'similarity': best_similarity,
        'contains': contains,
        'exact': exact,
        'year_ok': year_ok,
    }


def _is_confident_match(meta):
    if meta['exact']:
        return True
    if meta['contains'] and meta['year_ok']:
        return True
    return meta['similarity'] >= 0.72 and meta['year_ok']

def _get_movie(mid, language=None, search=False):
    details = None if search else \
        'trailers,images,releases,casts,keywords' if language is not None else \
        'trailers,images'
    return tmdbapi.get_movie(mid, language=language, append_to_response=details)

def _get_moviecollection(collection_id, language=None):
    if not collection_id:
        return None
    details = 'images'
    return tmdbapi.get_collection(collection_id, language=language, append_to_response=details)

def _parse_artwork(movie, collection, urlbases, language, proxy_prefix=''):
    if language:
        # Image languages don't have regional variants
        language = language.split('-')[0]
    posters = []
    landscape = []
    logos = []
    fanart = []

    if 'images' in movie:
        posters = _build_image_list_with_fallback(movie['images']['posters'], urlbases, language, proxy_prefix=proxy_prefix)
        landscape = _build_image_list_with_fallback(movie['images']['backdrops'], urlbases, language, proxy_prefix=proxy_prefix)
        logos = _build_image_list_with_fallback(movie['images']['logos'], urlbases, language, proxy_prefix=proxy_prefix)
        fanart = _build_fanart_list(movie['images']['backdrops'], urlbases, proxy_prefix=proxy_prefix)

    # Ensure TMDB's default poster (selected by language) is first
    if movie.get('poster_path') and posters:
        default_poster = movie['poster_path']
        for i, p in enumerate(posters):
            if p['url'].endswith(default_poster):
                if i != 0:
                    posters.insert(0, posters.pop(i))
                break

    setposters = []
    setlandscape = []
    setfanart = []
    if collection and 'images' in collection:
        setposters = _build_image_list_with_fallback(collection['images']['posters'], urlbases, language, proxy_prefix=proxy_prefix)
        setlandscape = _build_image_list_with_fallback(collection['images']['backdrops'], urlbases, language, proxy_prefix=proxy_prefix)
        setfanart = _build_fanart_list(collection['images']['backdrops'], urlbases, proxy_prefix=proxy_prefix)

    return {'poster': posters, 'landscape': landscape, 'fanart': fanart,
        'set.poster': setposters, 'set.landscape': setlandscape, 'set.fanart': setfanart, 'clearlogo': logos}

def _build_image_list_with_fallback(imagelist, urlbases, language, language_fallback='en', proxy_prefix=''):
    images = _build_image_list(imagelist, urlbases, [language], proxy_prefix=proxy_prefix)

    # Add backup images
    if language != language_fallback:
        images.extend(_build_image_list(imagelist, urlbases, [language_fallback], proxy_prefix=proxy_prefix))

    # Add any images if nothing set so far
    if not images:
        images = _build_image_list(imagelist, urlbases, proxy_prefix=proxy_prefix)

    return images

def _build_fanart_list(imagelist, urlbases, proxy_prefix=''):
    return _build_image_list(imagelist, urlbases, ['xx', None], proxy_prefix=proxy_prefix)

def _build_image_list(imagelist, urlbases, languages=[], proxy_prefix=''):
    result = []
    for img in imagelist:
        if languages and img['iso_639_1'] not in languages:
            continue
        if img['file_path'].endswith('.svg'):
            continue
        result.append({
            'url': proxy_prefix + urlbases['original'] + img['file_path'],
            'preview': proxy_prefix + urlbases['preview'] + img['file_path'],
            'lang': img['iso_639_1']
        })
    return result

def _get_date_numeric(datetime_):
    return (datetime_ - datetime(1970, 1, 1)).total_seconds()

def _load_base_urls(url_settings):
    urls = {}
    urls['original'] = url_settings.getSettingString('originalUrl')
    urls['preview'] = url_settings.getSettingString('previewUrl')
    last_updated = url_settings.getSettingString('lastUpdated')
    if not urls['original'] or not urls['preview'] or not last_updated or \
            float(last_updated) < _get_date_numeric(datetime.now() - timedelta(days=30)):
        conf = tmdbapi.get_configuration()
        if conf:
            urls['original'] = conf['images']['secure_base_url'] + 'original'
            urls['preview'] = conf['images']['secure_base_url'] + 'w780'
            url_settings.setSetting('originalUrl', urls['original'])
            url_settings.setSetting('previewUrl', urls['preview'])
            url_settings.setSetting('lastUpdated', str(_get_date_numeric(datetime.now())))
    return urls

_MAOYAN_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}

def _search_maoyan_movie_id(keyword, year):
    resp = api_utils.get(
        'https://apis.netstart.cn/maoyan/search/movies?keyword={}&ci=1'.format(quote(keyword)),
        timeout=10, headers=_MAOYAN_HEADERS)
    if resp.status_code != 200:
        return None
    data = resp.json()
    if not data or not isinstance(data, list) or len(data) == 0:
        return None

    if len(data) == 1:
        return data[0].get('id')

    if year:
        by_year = [item for item in data
                   if (item.get('release') or '').startswith(year)]
        if len(by_year) == 1:
            return by_year[0].get('id')

        keyword2 = '{} ({})'.format(keyword, year)
        resp2 = api_utils.get(
            'https://apis.netstart.cn/maoyan/search/movies?keyword={}&ci=1'.format(quote(keyword2)),
            timeout=10, headers=_MAOYAN_HEADERS)
        if resp2.status_code == 200:
            data2 = resp2.json()
            if data2 and isinstance(data2, list) and len(data2) > 0:
                return data2[0].get('id')

        return data[0].get('id')

    return data[0].get('id')


def _fetch_maoyan_trailer(title, release_date):
    year = ''
    if release_date:
        year = release_date[:4]
    try:
        movie_id = None

        prefix = re.split(r'[：\-]', title)[0].strip()
        if prefix != title:
            movie_id = _search_maoyan_movie_id(prefix, year)
            if movie_id:
                detail_url = 'https://apis.netstart.cn/maoyan/movie/detail?movieId={}'.format(movie_id)
                resp = api_utils.get(detail_url, timeout=10, headers=_MAOYAN_HEADERS)
                if resp.status_code == 200:
                    matches = re.findall(r'https?://vod\.pipi\.cn/[^\s"\']+\.mp4', resp.text)
                    if matches:
                        return matches[0]

        movie_id = _search_maoyan_movie_id(title, year)
        if not movie_id:
            return None

        detail_url = 'https://apis.netstart.cn/maoyan/movie/detail?movieId={}'.format(movie_id)
        resp = api_utils.get(detail_url, timeout=10, headers=_MAOYAN_HEADERS)
        if resp.status_code != 200:
            return None
        matches = re.findall(r'https?://vod\.pipi\.cn/[^\s"\']+\.mp4', resp.text)
        if matches:
            return matches[0]
    except Exception as e:
        try:
            tmdbapi.log(f'maoyan trailer failed for "{title}" ({year}): {e}')
        except Exception:
            pass
    return None

def _get_names(items):
    return [item['name'] for item in items] if items else []

def _get_cast_members(casts, casttype, department, jobs, image_proxy='', base_url=''):
    result = []
    seen = set()
    if casttype in casts:
        for cast in casts[casttype]:
            if cast['department'] == department and cast['job'] in jobs and cast['name'] not in seen:
                seen.add(cast['name'])
                thumb = ''
                if image_proxy and base_url and cast.get('profile_path'):
                    thumb = image_proxy + base_url + cast['profile_path']
                result.append({'name': cast['name'], 'thumbnail': thumb})
    return result
