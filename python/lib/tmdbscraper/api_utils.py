# coding: utf-8
#
# Copyright (C) 2020, Team Kodi
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Functions to interact with various web site APIs."""

from __future__ import absolute_import, unicode_literals

import json
import socket
import requests
from urllib.parse import urlparse

try:
    import xbmc
    import xbmcgui
except ModuleNotFoundError:
    # only used for logging HTTP calls, not available nor needed for testing
    xbmc = None
    xbmcgui = None

# from pprint import pformat
try: #PY2 / PY3
    from urllib2 import Request, urlopen
    from urllib2 import URLError
    from urllib import urlencode
except ImportError:
    from urllib.request import Request, urlopen
    from urllib.error import URLError
    from urllib.parse import urlencode
try:
    from typing import Text, Optional, Union, List, Dict, Any  # pylint: disable=unused-import
    InfoType = Dict[Text, Any]  # pylint: disable=invalid-name
except ImportError:
    pass

HEADERS = {}
DNS_SETTINGS = {}
SERVICE_HOST = '127.0.0.1'

def set_headers(headers):
    HEADERS.clear()
    HEADERS.update(headers)

def set_dns_settings(settings):
    DNS_SETTINGS.clear()
    if settings:
        DNS_SETTINGS.update(settings)

def load_info_from_service(url, params=None, headers=None, batch_payload=None, dns_settings=None):
    """
    Send request to the background service daemon via TCP socket.
    Supports single request (url, params) or batch request (batch_payload).
    """
    try:
        # Get port dynamically from Window Property
        service_port = 56789 # Default fallback
        if xbmcgui:
            port_str = xbmcgui.Window(10000).getProperty('TMDB_OPTIMIZATION_SERVICE_PORT')
            if port_str:
                service_port = int(port_str)
            else:
                return {'error': 'Service port not found in Window Property'}
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(35) # Slightly longer than service timeout
        sock.connect((SERVICE_HOST, service_port))
        
        # Construct Protocol V2 Payload
        if batch_payload:
            requests_list = batch_payload
        else:
            requests_list = [{
                'url': url,
                'params': params,
                'headers': headers or {}
            }]
            
        request_data = {
            'requests': requests_list,
            'dns_settings': dns_settings or DNS_SETTINGS
        }
        
        sock.sendall(json.dumps(request_data).encode('utf-8'))
        
        # Read response
        response_data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response_data += chunk
            
        sock.close()
        
        if not response_data:
            return {'error': 'Empty response from service'}
            
        result = json.loads(response_data)
        
        # If it was a single request call (not batch_payload), unwrap the list result
        if not batch_payload and isinstance(result, list) and len(result) == 1:
            return result[0]
            
        return result
        
    except Exception as e:
        
        if isinstance(result, dict) and 'error' in result:
            return {'error': result['error']}
            
        return result # Contains 'text', 'json', 'status' or list of results
        
    except Exception as e:
        if xbmc:
            xbmc.log('[TMDB Scraper] Service IPC Error: {}'.format(e), xbmc.LOGERROR)
        return {'error': 'Service communication failed: {}'.format(e)}
            
        return result # Contains 'text', 'json', 'status'
        
    except Exception as e:
        if xbmc:
            xbmc.log('[TMDB Scraper] Service IPC Error: {}'.format(e), xbmc.LOGERROR)
        return {'error': 'Service communication failed: {}'.format(e)}

def load_info(url, params=None, default=None, resp_type = 'json'):
    # type: (Text, Optional[Dict[Text, Union[Text, List[Text]]]]) -> Union[dict, list]
    """
    Load info from external api using persistent service daemon

    :param url: API endpoint URL
    :param params: URL query params
    :default: object to return if there is an error
    :resp_type: what to return to the calling function
    :return: API response or default on error
    """
    theerror = ''
    
    if xbmc:
        # Log the request for debugging
        log_url = url
        if params:
            log_url += '?' + urlencode(params)
        xbmc.log('Calling URL "{}"'.format(log_url), xbmc.LOGDEBUG)
        if HEADERS:
            xbmc.log(str(HEADERS), xbmc.LOGDEBUG)
            
    # Try to use the service first
    service_result = load_info_from_service(url, params, HEADERS)
    
    if 'error' not in service_result:
        # Success
        if resp_type.lower() == 'json':
            return service_result.get('json') or json.loads(service_result.get('text', '{}'))
        else:
            return service_result.get('text')
    else:
        # Fallback to direct request if service fails (e.g. not running)
        if xbmc:
            xbmc.log('[TMDB Scraper] -----Service unavailable ({}), falling back to direct request'.format(service_result['error']), xbmc.LOGWARNING)
            
        try:
            # Direct request (non-persistent session, or local session)
            resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            
            if resp_type.lower() == 'json':
                return resp.json()
            else:
                return resp.text
                
        except Exception as e:
            theerror = {'error': 'Direct request failed: {}'.format(e)}
            if default is not None:
                return default
            return theerror
