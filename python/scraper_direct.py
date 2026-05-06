# coding: utf-8
import sys
import os

try:
    import xbmc
    import xbmcaddon
except ImportError:
    xbmc = None
    xbmcaddon = None

from lib.tmdbscraper_direct.tmdb import TMDBMovieScraper
from lib.tmdbscraper_direct import fanarttv
from lib.tmdbscraper_direct import imdbratings
from lib.tmdbscraper_direct import traktratings

from scraper_datahelper import combine_scraped_details_info_and_ratings, \
    combine_scraped_details_available_artwork
from scraper_config import configure_scraped_details, configure_tmdb_artwork, is_fanarttv_configured
from title_search_optimizer import build_search_title_candidates

INCLUDE_ADULT_CONTENT = False

class ScraperRunner(object):
    def __init__(self, settings):
        
        self.settings = settings
        self._init_scraper()

    def _init_scraper(self):
        # Provide defaults if settings fail (e.g. mock object incomplete)
        try:
            language = self.settings.getSettingString('language')
        except:
            language = 'zh'
            
        try:
            cert_country = self.settings.getSettingString('tmdbcertcountry')
        except:
            cert_country = 'CN'
            
        try:
            search_language = self.settings.getSettingString('searchlanguage')
        except:
            search_language = language
            
        self.tmdb = TMDBMovieScraper(
            url_settings=self.settings,
            language=language,
            certification_country=cert_country,
            search_language=search_language,
            include_adult=INCLUDE_ADULT_CONTENT
        )

    def search(self, title, year=None):
        """
        Run search and return raw list of dicts.
        Matches logic in scraper.py search_for_movie (strips articles, fallbacks for year)
        """
        search_results, _, _ = self.search_with_history(title, year)
        return search_results

    def search_with_history(self, title, year=None):
        """
        Run search and return (results, history, matched_query_title).
        History includes every candidate and year-fallback query actually executed.
        """
        history = []

        for candidate in build_search_title_candidates(title):
            candidate_title = self._strip_trailing_article(candidate)
            search_results, attempts = self._search_with_year_fallback_with_history(candidate_title, year)

            for attempt_year, attempt_result in attempts:
                year_text = "None" if attempt_year is None else str(attempt_year)
                if isinstance(attempt_result, dict) and 'error' in attempt_result:
                    history.append(
                        "候选: {0} -> 查询: {1} ({2}) 错误: {3}".format(
                            candidate,
                            candidate_title,
                            year_text,
                            attempt_result.get('error', ''),
                        )
                    )
                else:
                    count = len(attempt_result) if attempt_result else 0
                    history.append(
                        "候选: {0} -> 查询: {1} ({2}) 结果: {3}".format(
                            candidate,
                            candidate_title,
                            year_text,
                            count,
                        )
                    )

            if isinstance(search_results, dict) and 'error' in search_results:
                return search_results, history, candidate_title

            if search_results:
                return search_results, history, candidate_title

        return [], history, None

    def _search_with_year_fallback(self, title, year):
        search_results, _ = self._search_with_year_fallback_with_history(title, year)
        return search_results

    def _search_with_year_fallback_with_history(self, title, year):
        attempts = []
        search_results = self.tmdb.search(title, year)
        attempts.append((year, search_results))

        if isinstance(search_results, dict) and 'error' in search_results:
            return search_results, attempts

        if year is not None and not search_results:
            try:
                fallback_years = [str(int(year) - 1), str(int(year) + 1), None]
            except Exception:
                fallback_years = [None]

            for fallback_year in fallback_years:
                search_results = self.tmdb.search(title, fallback_year)
                attempts.append((fallback_year, search_results))
                if isinstance(search_results, dict) and 'error' in search_results:
                    break
                if search_results:
                    break

        return search_results, attempts

    def _strip_trailing_article(self, title):
        _articles = [prefix + article for prefix in (', ', ' ') for article in ("the", "a", "an")]
        title = title.lower()
        for article in _articles:
            if title.endswith(article):
                return title[:-len(article)]
        return title

    def get_details(self, uniqueids):
        """
        Args:
            uniqueids (dict): e.g. {'tmdb': '12345', 'imdb': 'tt12345'}
        Returns:
            dict: Scraper details dict
        """
        # 1. Get Base TMDB Details
        details = self.tmdb.get_details(uniqueids)
        
        if not details or details.get('error'):
            return details
            
        # Update uniqueids with any new finding (e.g. IMDb ID found in TMDB)
        curr_uniqueids = details.get('uniqueids', {})

        # 2. Additional Artwork (Fanart.tv)
        if is_fanarttv_configured(self.settings):
            client_key = self.settings.getSettingString('fanarttv_clientkey')
            set_tmdbid = details.get('_info', {}).get('set_tmdbid')
            
            try:
                fanart_art = fanarttv.get_details(curr_uniqueids, client_key, self.tmdb.language, set_tmdbid, self.settings)
                combine_scraped_details_available_artwork(details, fanart_art, self.tmdb.language, self.settings)
            except Exception:
                pass

        # 3. Ratings (IMDb / Trakt)
        
        # Check if we should fetch IMDb
        fetch_imdb = False
        try:
            if self.settings.getSettingString('RatingS') == 'IMDb' or self.settings.getSettingBool('imdbanyway'):
                fetch_imdb = True
        except:
            pass 

        if fetch_imdb:
            try:
                imdb_res = imdbratings.get_details(curr_uniqueids, self.settings)
                combine_scraped_details_info_and_ratings(details, imdb_res)
            except:
                pass

        # Check if we should fetch Trakt
        fetch_trakt = False
        try:
            if self.settings.getSettingString('RatingS') == 'Trakt' or self.settings.getSettingBool('traktanyway'):
                fetch_trakt = True
        except:
             pass

        if fetch_trakt:
            try:
                trakt_res = traktratings.get_trakt_ratinginfo(curr_uniqueids, self.settings)
                combine_scraped_details_info_and_ratings(details, trakt_res)
            except:
                pass

        # 4. Final Configuration
        details = configure_tmdb_artwork(details, self.settings)
        details = configure_scraped_details(details, self.settings)

        return details
