"""Add optional wsrv.nl width limits to scraper image URLs."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class ImageResizer:
    def __init__(self, settings):
        self.enabled = False
        self.prefix = ''
        self.widths = {}
        if settings is None:
            return
        try:
            if settings.getSettingBool('enable_image_resize') is not True:
                return
            self.prefix = (settings.getSettingString('image_proxy_prefix') or
                           'https://wsrv.nl/?url=')
            proxy = urlsplit(self.prefix)
            if proxy.scheme not in ('http', 'https') or proxy.hostname != 'wsrv.nl':
                return
        except (AttributeError, TypeError, ValueError):
            return

        for art_type in ('poster', 'keyart', 'fanart', 'landscape', 'banner',
                         'discart', 'clearart', 'clearlogo', 'thumb'):
            try:
                value = settings.getSettingString('image_max_width_' + art_type).strip()
                if value.isdecimal() and int(value) > 0:
                    self.widths[art_type] = int(value)
            except (AttributeError, TypeError, ValueError):
                continue
        self.enabled = True

    def resize(self, image_url, art_type):
        if art_type.startswith('set.'):
            art_type = art_type[4:]
        width = self.widths.get(art_type)
        if not self.enabled or not width or not image_url:
            return image_url

        try:
            parts = urlsplit(image_url)
            if parts.scheme not in ('http', 'https') or parts.hostname != 'wsrv.nl':
                return image_url

            source_url = image_url[len(self.prefix):] if image_url.startswith(self.prefix) else ''
            if (self.prefix.endswith('url=') and
                    source_url.startswith(('http://', 'https://'))):
                # Existing scraper URLs concatenate the source URL verbatim.
                # Keep its query/fragment inside the encoded url parameter.
                parts = urlsplit(self.prefix)
                params = [(key, source_url if key == 'url' else value)
                          for key, value in parse_qsl(parts.query, keep_blank_values=True)]
            else:
                params = parse_qsl(parts.query, keep_blank_values=True)

            if not any(key == 'url' and value for key, value in params):
                return image_url
            # Replace existing dimensions so the configured width stays a maximum.
            params = [(key, value) for key, value in params
                      if key not in ('w', 'h', 'dpr', 'fit', 'we')]
            params.append(('w', str(width)))
            return urlunsplit(parts._replace(query=urlencode(params) + '&we'))
        except (TypeError, ValueError):
            return image_url

    def configure_details(self, details):
        if not self.enabled:
            return details
        for art_type, images in details.get('available_art', {}).items():
            for image in images:
                for key in ('url', 'preview'):
                    if image.get(key):
                        image[key] = self.resize(image[key], art_type)

        people = list(details.get('cast', []))
        for key in ('director', 'credits'):
            people.extend(details.get('info', {}).get(key, []))
        for person in people:
            if isinstance(person, dict) and person.get('thumbnail'):
                person['thumbnail'] = self.resize(person['thumbnail'], 'thumb')
        return details
