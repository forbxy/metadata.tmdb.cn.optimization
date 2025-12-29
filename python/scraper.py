import json
import sys
import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

from lib.tmdbscraper.tmdb import TMDBMovieScraper
from lib.tmdbscraper import fanarttv
from lib.tmdbscraper import imdbratings
from lib.tmdbscraper import traktratings
from lib.tmdbscraper import api_utils
from lib.tmdbscraper import tmdbapi

from scraper_datahelper import combine_scraped_details_info_and_ratings, \
    combine_scraped_details_available_artwork, find_uniqueids_in_text, get_params
from scraper_config import configure_scraped_details, PathSpecificSettings, \
    configure_tmdb_artwork, is_fanarttv_configured

ADDON_SETTINGS = xbmcaddon.Addon()
ID = ADDON_SETTINGS.getAddonInfo('id')

def log(msg, level=xbmc.LOGDEBUG):
    xbmc.log(msg='[{addon}]: {msg}'.format(addon=ID, msg=msg), level=level)

def get_tmdb_scraper(settings):
    language = settings.getSettingString('language')
    certcountry = settings.getSettingString('tmdbcertcountry')
    search_language = settings.getSettingString('searchlanguage')
    return TMDBMovieScraper(settings, language, certcountry, search_language)

def get_dns_settings(settings):
    dns_map = {}
    settings_map = {
        'dns_tmdb_api': 'api.tmdb.org',
        'dns_fanart_tv': 'webservice.fanart.tv',
        'dns_imdb_www': 'www.imdb.com',
        'dns_trakt_tv': 'trakt.tv'
    }
    for setting_id, domain in settings_map.items():
        ip = settings.getSettingString(setting_id).strip()
        dns_map[domain] = ip
    return dns_map

def search_for_movie(title, year, handle, settings):
    log("Find movie with title '{title}' from year '{year}'".format(title=title, year=year), xbmc.LOGINFO)
    title = _strip_trailing_article(title)
    scraper = get_tmdb_scraper(settings)

    search_results = scraper.search(title, year)
    if year is not None:
        if not search_results:
            search_results = scraper.search(title,str(int(year)-1))
        if not search_results:
            search_results = scraper.search(title,str(int(year)+1))
        if not search_results:
            search_results = scraper.search(title)
    if not search_results:
        return

    if 'error' in search_results:
        header = "The Movie Database Python error searching with web service TMDB"
        xbmcgui.Dialog().notification(header, search_results['error'], xbmcgui.NOTIFICATION_WARNING)
        log(header + ': ' + search_results['error'], xbmc.LOGWARNING)
        return

    for movie in search_results:
        listitem = _searchresult_to_listitem(movie)
        uniqueids = {'tmdb': str(movie['id'])}
        xbmcplugin.addDirectoryItem(handle=handle, url=build_lookup_string(uniqueids),
            listitem=listitem, isFolder=True)

_articles = [prefix + article for prefix in (', ', ' ') for article in ("the", "a", "an")]
def _strip_trailing_article(title):
    title = title.lower()
    for article in _articles:
        if title.endswith(article):
            return title[:-len(article)]
    return title

def _searchresult_to_listitem(movie):
    movie_label = movie['title']

    movie_year = movie['release_date'].split('-')[0] if movie.get('release_date') else None
    if movie_year:
        movie_label += ' ({})'.format(movie_year)

    listitem = xbmcgui.ListItem(movie_label, offscreen=True)

    infotag = listitem.getVideoInfoTag()
    infotag.setTitle(movie['title'])
    if movie_year:
        infotag.setYear(int(movie_year))

    if movie['poster_path']:
        listitem.setArt({'thumb': movie['poster_path']})

    return listitem

# Default limit of 10 because a big list of artwork can cause trouble in some cases
# (a column can be too large for the MySQL integration),
# and how useful is a big list anyway? Not exactly rhetorical, this is an experiment.
def add_artworks(listitem, artworks, IMAGE_LIMIT):
    infotag = listitem.getVideoInfoTag()
    for arttype, artlist in artworks.items():
        if arttype == 'fanart':
            continue
        for image in artlist[:IMAGE_LIMIT]:
            infotag.addAvailableArtwork(image['url'], arttype)

    fanart_to_set = [{'image': image['url'], 'preview': image['preview']}
        for image in artworks.get('fanart', ())[:IMAGE_LIMIT]]
    listitem.setAvailableFanart(fanart_to_set)


def get_details(input_uniqueids, handle, settings, fail_silently=False):
    if not input_uniqueids:
        return False

    tmdb_scraper = get_tmdb_scraper(settings)
    
    # Step 0: Resolve TMDB ID if missing
    tmdb_id = input_uniqueids.get('tmdb')
    if not tmdb_id and input_uniqueids.get('imdb'):
        find_results = tmdbapi.find_movie_by_external_id(input_uniqueids['imdb'])
        if find_results.get('movie_results'):
            tmdb_id = str(find_results['movie_results'][0]['id'])
            input_uniqueids['tmdb'] = tmdb_id

    # Step 1: Build Batch
    batch_requests = []
    
    if tmdb_id:
        batch_requests.extend(tmdb_scraper.get_movie_requests(tmdb_id))
        if is_fanarttv_configured(settings):
             batch_requests.extend(fanarttv.get_movie_requests(input_uniqueids, 
                settings.getSettingString('fanarttv_clientkey'), None, settings=settings))

    if settings.getSettingString('RatingS') == 'IMDb' or settings.getSettingBool('imdbanyway'):
        # xbmc.log("Adding IMDb rating request to batch", xbmc.LOGINFO)
        batch_requests.extend(imdbratings.get_movie_requests(input_uniqueids, settings=settings))

    if settings.getSettingString('RatingS') == 'Trakt' or settings.getSettingBool('traktanyway'):
        batch_requests.extend(traktratings.get_movie_requests(input_uniqueids, settings=settings))

    if not batch_requests:
        return False

    # Step 2: Send Batch
    responses_list = api_utils.load_info_from_service(None, batch_payload=batch_requests)
    
    if isinstance(responses_list, dict) and 'error' in responses_list:
        log("Service error: " + responses_list['error'], xbmc.LOGERROR)
        if fail_silently:
            return False
        # Fallback or notify?
        # For now, just return False
        return False

    # Step 3: Map Results
    responses_by_type = {}
    for i, req in enumerate(batch_requests):
        if i < len(responses_list):
            res = responses_list[i]
            val = None
            if req.get('resp_type') == 'text':
                val = res.get('text')
            else:
                val = res.get('json')
                if val is None and res.get('text'):
                    try:
                        val = json.loads(res['text'])
                    except:
                        pass
            responses_by_type[req['type']] = val

    # Step 4: Process Results
    details = {}
    
    if tmdb_id:
        details = tmdb_scraper.parse_movie_response(responses_by_type)
        if not details or details.get('error'):
             if fail_silently:
                 return False
             header = "The Movie Database Python error with web service TMDB"
             err = details.get('error', 'Unknown error') if details else 'No details'
             xbmcgui.Dialog().notification(header, err, xbmcgui.NOTIFICATION_WARNING)
             log(header + ': ' + err, xbmc.LOGWARNING)
             return False
             
        # Prepare Secondary Batch (Collections + Late-bound IMDb/Trakt)
        batch_secondary = []
        
        # 1. Collections
        collection_id = details.get('_info', {}).get('set_tmdbid')
        if collection_id:
            batch_secondary.extend(tmdb_scraper.get_collection_requests(collection_id))
            
            if is_fanarttv_configured(settings):
                fanart_reqs = fanarttv.get_movie_requests(input_uniqueids, 
                    settings.getSettingString('fanarttv_clientkey'),
                    collection_id, settings=settings)
                # Only add collection requests
                batch_secondary.extend([r for r in fanart_reqs if r['type'] == 'fanart_collection'])

        # 2. Late-bound IMDb/Trakt
        # If we didn't have IMDb ID initially, but TMDB returned one, we fetch it now
        new_uniqueids = details.get('uniqueids', {})
        new_imdb_id = new_uniqueids.get('imdb')
        
        if new_imdb_id and new_imdb_id != input_uniqueids.get('imdb'):
            # Update input_uniqueids so get_movie_requests works
            input_uniqueids['imdb'] = new_imdb_id
            
            # Check IMDb
            if settings.getSettingString('RatingS') == 'IMDb' or settings.getSettingBool('imdbanyway'):
                # We know we didn't request it before because we didn't have the ID
                batch_secondary.extend(imdbratings.get_movie_requests(input_uniqueids, settings=settings))

            # Check Trakt
            if settings.getSettingString('RatingS') == 'Trakt' or settings.getSettingBool('traktanyway'):
                batch_secondary.extend(traktratings.get_movie_requests(input_uniqueids, settings=settings))
        
        # Execute Secondary Batch
        if batch_secondary:
            sec_responses = api_utils.load_info_from_service(None, batch_payload=batch_secondary)
            
            sec_responses_by_type = {}
            for i, req in enumerate(batch_secondary):
                if i < len(sec_responses):
                    res = sec_responses[i]
                    val = None
                    if req.get('resp_type') == 'text':
                        val = res.get('text')
                    else:
                        val = res.get('json')
                        if val is None and res.get('text'):
                            try:
                                val = json.loads(res['text'])
                            except:
                                pass
                    sec_responses_by_type[req['type']] = val
            
            # Merge results
            responses_by_type.update(sec_responses_by_type)
            
            # Re-parse TMDB if we fetched collection info
            if collection_id:
                details = tmdb_scraper.parse_movie_response(responses_by_type)

    # Process IMDb
    imdb_info = imdbratings.parse_movie_response(responses_by_type)
    if imdb_info:
        if 'error' in imdb_info:
             log("IMDb error: " + imdb_info['error'], xbmc.LOGWARNING)
        else:
             details = combine_scraped_details_info_and_ratings(details, imdb_info)

    # Process Trakt
    trakt_info = traktratings.parse_movie_response(responses_by_type)
    if trakt_info:
        details = combine_scraped_details_info_and_ratings(details, trakt_info)
        
    # Fanart
    fanart_info = fanarttv.parse_movie_response(responses_by_type, settings.getSettingString('language'), settings=settings)
    if fanart_info:
        details = combine_scraped_details_available_artwork(details,
            fanart_info,
            settings.getSettingString('language'),
            settings)

    details = configure_scraped_details(details, settings)

    listitem = xbmcgui.ListItem(details['info']['title'], offscreen=True)
    infotag = listitem.getVideoInfoTag()
    set_info(infotag, details['info'])
    infotag.setCast(build_cast(details['cast']))
    infotag.setUniqueIDs(details['uniqueids'], 'tmdb')
    infotag.setRatings(build_ratings(details['ratings']), find_defaultrating(details['ratings']))
    IMAGE_LIMIT = settings.getSettingInt('maxartwork')
    add_artworks(listitem, details['available_art'], IMAGE_LIMIT)

    xbmcplugin.setResolvedUrl(handle=handle, succeeded=True, listitem=listitem)
    return True

def set_info(infotag: xbmc.InfoTagVideo, info_dict):
    infotag.setTitle(info_dict['title'])
    infotag.setOriginalTitle(info_dict['originaltitle'])
    infotag.setPlot(info_dict['plot'])
    infotag.setTagLine(info_dict['tagline'])
    infotag.setStudios(info_dict['studio'])
    infotag.setGenres(info_dict['genre'])
    infotag.setCountries(info_dict['country'])
    infotag.setWriters(info_dict['credits'])
    infotag.setDirectors(info_dict['director'])
    infotag.setPremiered(info_dict['premiered'])
    if 'tag' in info_dict:
        infotag.setTags(info_dict['tag'])
    if 'mpaa' in info_dict:
        infotag.setMpaa(info_dict['mpaa'])
    if 'trailer' in info_dict:
        infotag.setTrailer(info_dict['trailer'])
    if 'set' in info_dict:
        infotag.setSet(info_dict['set'])
        infotag.setSetOverview(info_dict['setoverview'])
    if 'duration' in info_dict:
        infotag.setDuration(info_dict['duration'])
    if 'top250' in info_dict:
        infotag.setTop250(info_dict['top250'])

def build_cast(cast_list):
    return [xbmc.Actor(cast['name'], cast['role'], cast['order'], cast['thumbnail']) for cast in cast_list]

def build_ratings(rating_dict):
    return {key: (value['rating'], value.get('votes', 0)) for key, value in rating_dict.items()}

def find_defaultrating(rating_dict):
    return next((key for key, value in rating_dict.items() if value['default']), None)

def find_uniqueids_in_nfo(nfo, handle):
    uniqueids = find_uniqueids_in_text(nfo)
    if uniqueids:
        listitem = xbmcgui.ListItem(offscreen=True)
        xbmcplugin.addDirectoryItem(
            handle=handle, url=build_lookup_string(uniqueids), listitem=listitem, isFolder=True)

def build_lookup_string(uniqueids):
    return json.dumps(uniqueids)

def parse_lookup_string(uniqueids):
    try:
        return json.loads(uniqueids)
    except ValueError:
        log("Can't parse this lookup string, is it from another add-on?\n" + uniqueids, xbmc.LOGWARNING)
        return None

def run():
    params = get_params(sys.argv[1:])
    enddir = True
    if 'action' in params:
        # log(params, xbmc.LOGINFO)
        settings = ADDON_SETTINGS if not params.get('pathSettings') else \
            PathSpecificSettings(json.loads(params['pathSettings']), lambda msg: log(msg, xbmc.LOGWARNING))
        
        # Extract and set DNS settings globally for api_utils
        dns_settings = get_dns_settings(settings)
        api_utils.set_dns_settings(dns_settings)
        action = params["action"]
        if action == 'find' and 'title' in params:
            search_for_movie(params["title"], params.get("year"), params['handle'], settings)
        elif action == 'getdetails' and ('url' in params or 'uniqueIDs' in params):
            unique_ids = parse_lookup_string(params.get('uniqueIDs') or params.get('url'))
            enddir = not get_details(unique_ids, params['handle'], settings, fail_silently='uniqueIDs' in params)
        elif action == 'NfoUrl' and 'nfo' in params:
            find_uniqueids_in_nfo(params["nfo"], params['handle'])
        else:
            log("unhandled action: " + action, xbmc.LOGWARNING)
    else:
        log("No action in 'params' to act on", xbmc.LOGWARNING)
    if enddir:
        xbmcplugin.endOfDirectory(params['handle'])

if __name__ == '__main__':
    
    run()
