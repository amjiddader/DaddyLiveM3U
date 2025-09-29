import re
import requests
import json
import html
from urllib.parse import urlparse, quote_plus, unquote
from datetime import datetime, timedelta, timezone
import threading
import base64

class DaddyLiveAPI:
    def __init__(self):
        self.baseurl = None
        self.json_url = None
        self.schedule_url = None
        self.UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36'

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': self.UA,
            'Connection': 'Keep-Alive'
        })

        self._initialize_base_urls()

        self.stream_cache = {}
        self.cache_expiry_minutes = 7
        self.cache_lock = threading.Lock()

    def _initialize_base_urls(self):
        try:
            main_url_content = self.session.get('https://raw.githubusercontent.com/thecrewwh/dl_url/refs/heads/main/dl.xml', timeout=5).text
            found_iframe_src = re.findall('src = "([^"]*)', main_url_content)

            if found_iframe_src:
                iframe_url = found_iframe_src[0]
                parsed_iframe_url = urlparse(iframe_url)
                self.baseurl = f"{parsed_iframe_url.scheme}://{parsed_iframe_url.netloc}"
            else:
                raise ValueError("Could not find baseurl in dl.xml")
        except Exception as e:
            print(f"Error initializing base URLs: {e}")
            self.baseurl = 'https://daddylivestream.com'

        self.json_url = f'{self.baseurl}/stream/stream-%s.php'
        self.schedule_url = f'{self.baseurl}/schedule/schedule-generated.php'

    def get_headers(self, referer_override=None, origin_override=None):
        headers = {
            'User-Agent': self.UA,
            'Connection': 'Keep-Alive'
        }
        headers['Referer'] = referer_override if referer_override else f'{self.baseurl}/'
        headers['Origin'] = origin_override if origin_override else self.baseurl
        return headers

    def _get_local_time(self, utc_time_str):
        try:
            utc_now = datetime.utcnow()
            event_time_utc = datetime.strptime(utc_time_str, '%H:%M')
            event_time_utc = event_time_utc.replace(year=utc_now.year, month=utc_now.month, day=utc_now.day)
            event_time_utc = event_time_utc.replace(tzinfo=timezone.utc)
            local_time = event_time_utc.astimezone()
            return local_time.strftime('%I:%M %p').lstrip('0')
        except Exception as e:
            print(f"Failed to convert time: {e}")
            return utc_time_str

    def get_all_streams(self):
        url = f'{self.baseurl}/24-7-channels.php'
        headers = self.get_headers()
        streams_list = []
        try:
            resp = self.session.get(url, headers=headers, timeout=10).text
            channel_items = re.findall(
                r'href="/stream/stream-(\d+)\.php"[^>]*>\s*(?:<[^>]+>)*([^<]+)',
                resp,
                re.DOTALL
            )
            for channel_id, name in channel_items:
                streams_list.append({
                    'name': html.unescape(name.strip()),
                    'id': channel_id
                })
        except Exception as e:
            print(f"Error fetching streams: {e}")
        return streams_list

    def get_scheduled_events(self):
        headers = self.get_headers()
        all_events = {}
        try:
            schedule = requests.get(self.schedule_url, headers=headers, timeout=10).json()
            for date_key, events_by_category in schedule.items():
                for categ, events_list in events_by_category.items():
                    category_name = categ.replace('</span>', '').strip()
                    if category_name not in all_events:
                        all_events[category_name] = []

                    for item in events_list:
                        event = item.get('event')
                        time_str = item.get('time')
                        event_time_local = self._get_local_time(time_str)
                        title = f'{event_time_local} {event}'

                        parsed_channels = []
                        for channel in item.get('channels', []):
                            if isinstance(channel, dict):
                                parsed_channels.append({
                                    'name': html.unescape(channel.get('channel_name', '')),
                                    'id': channel.get('channel_id', '')
                                })

                        all_events[category_name].append({
                            'title': title,
                            'channels': parsed_channels
                        })
        except Exception as e:
            print(f"Error fetching scheduled events: {e}")
        return all_events

    def resolve_stream(self, channel_id):
        with self.cache_lock:
            cached = self.stream_cache.get(channel_id)
            if cached:
                url, headers, timestamp = cached
                if datetime.now() - timestamp < timedelta(minutes=self.cache_expiry_minutes):
                    return url, headers
                del self.stream_cache[channel_id]

        if not self.baseurl:
            self._initialize_base_urls()

        url_stream = self.json_url % channel_id
        headers = self.get_headers()

        try:
            response = requests.get(url_stream, headers=headers, timeout=10).text
            
            # Try multiple patterns to find the player link
            player_patterns = [
                r'<a[^>]*href="([^"]+)"[^>]*>\s*<button[^>]*>\s*Player\s*2\s*</button>',
                r'<a[^>]*href="(/cast[^"]+)"[^>]*>\s*<button',
                r'href="(/cast[^"]*)"',
                r'<a[^>]*href="([^"]+)"[^>]*>\s*<button[^>]*>.*?[Pp]layer.*?</button>',
                r'<iframe[^>]*src="([^"]+)"',
            ]
            
            url2 = None
            for pattern in player_patterns:
                iframes = re.findall(pattern, response, re.IGNORECASE | re.DOTALL)
                if iframes:
                    url2 = iframes[0]
                    break
            
            if not url2:
                print(f"No player link found for channel {channel_id}")
                return None, None

            if not url2.startswith('http'):
                url2 = self.baseurl + url2
            url2 = url2.replace('//cast','/cast')

            headers['Referer'] = url2
            headers['Origin'] = urlparse(url2).scheme + "://" + urlparse(url2).netloc
            response = requests.get(url2, headers=headers, timeout=10).text

            iframes = re.findall(r'iframe\s+src="([^"]*)', response, re.IGNORECASE)
            if not iframes:
                print("No iframe src found in url2 response.")
                return None, None

            url3 = iframes[0]
            if not url3.startswith('http'):
                url3 = f"https://{urlparse(headers['Referer']).netloc}{url3}"

            headers['Referer'] = url3
            headers['Origin'] = urlparse(url3).scheme + "://" + urlparse(url3).netloc
            response = requests.get(url3, headers=headers, timeout=10).text

            # Extract channel_key (NOT base64 encoded)
            channel_key_match = re.search(r'const\s+CHANNEL_KEY\s*=\s*"([^"]+)"', response)
            if not channel_key_match:
                channel_key_match = re.search(r'channelKey\s*=\s*["\']([^"\']+)["\']', response)
                if not channel_key_match:
                    print("Could not find CHANNEL_KEY in response.")
                    return None, None
            channel_key = channel_key_match.group(1)

            # Extract the bundled parameters (XJZ bundle - base64 encoded JSON)
            bundle_match = re.search(r'const\s+XJZ\s*=\s*"([^"]+)"', response)
            if not bundle_match:
                print("Could not find XJZ bundle in response.")
                return None, None
            
            bundle = bundle_match.group(1)
            parts = json.loads(base64.b64decode(bundle).decode("utf-8"))
            
            # Now decode each part from base64
            for k, v in parts.items():
                parts[k] = base64.b64decode(v).decode("utf-8")

            # Extract host array
            host_array_match = re.search(r"host\s*=\s*\[([^\]]+)\]", response)
            if not host_array_match:
                print("Could not find host array in response.")
                return None, None
            
            host_parts = [part.strip().strip("'\"") for part in host_array_match.group(1).split(',')]
            host = ''.join(host_parts)

            # Construct the authentication script path by XORing bytes
            bx = [40, 60, 61, 33, 103, 57, 33, 57]
            sc = ''.join(chr(b ^ 73) for b in bx)

            # Build authentication URL
            auth_url = (
                f'{host}{sc}?channel_id={quote_plus(channel_key)}&'
                f'ts={quote_plus(parts["b_ts"])}&'
                f'rnd={quote_plus(parts["b_rnd"])}&'
                f'sig={quote_plus(parts["b_sig"])}'
            )

            # Call authentication endpoint
            requests.get(auth_url, headers=headers, timeout=10)

            # Get server lookup URL
            server_lookup_match = re.findall(r'fetchWithRetry\(\s*["\']([^"\']*)', response)
            if not server_lookup_match:
                print("Could not find server lookup URL in response.")
                return None, None
            server_lookup = server_lookup_match[0]

            # Get server key
            server_lookup_url = f"https://{urlparse(url3).netloc}{server_lookup}{channel_key}"
            server_response = requests.get(server_lookup_url, headers=headers, timeout=10).json()
            server_key = server_response.get('server_key')

            if not server_key:
                print("Could not get server_key from server lookup.")
                return None, None

            # Construct final HLS URL based on server_key
            host_raw = f"https://{urlparse(url3).netloc}"
            if server_key == "top1/cdn":
                final_hls_url = f"https://top1.newkso.ru/top1/cdn/{channel_key}/mono.m3u8"
            else:
                final_hls_url = f"https://{server_key}new.newkso.ru/{server_key}/{channel_key}/mono.m3u8"

            hls_headers = {
                'Referer': f"{host_raw}/",
                'Origin': host_raw,
                'User-Agent': self.UA,
                'Connection': 'keep-alive'
            }

            with self.cache_lock:
                self.stream_cache[channel_id] = (final_hls_url, hls_headers, datetime.now())

            return final_hls_url, hls_headers

        except Exception as e:
            import traceback
            print(f"Error resolving stream: {e}\n{traceback.format_exc()}")
            return None, None

daddylive_api = DaddyLiveAPI()