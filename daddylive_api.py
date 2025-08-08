import re
import requests
import json
import html
from urllib.parse import urlparse, quote_plus, unquote
from datetime import datetime, timedelta, timezone
import threading
import base64 # Import base64 module

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
            self.baseurl = 'https://daddylive.dad'

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
            print(f"DEBUG: Found {len(channel_items)} stream entries via updated regex.")
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
            iframes = re.findall(r'<a[^>]*href="([^"]+)"[^>]*>\s*<button[^>]*>\s*Player\s*2\s*</button>', response)
            if not iframes:
                print("No Player 2 iframe found in initial response.")
                return None, None

            url2 = iframes[0]
            if not url2.startswith('http'):
                url2 = self.baseurl + url2
            url2 = url2.replace('//cast','/cast')

            headers['Referer'] = url2
            headers['Origin'] = urlparse(url2).scheme + "://" + urlparse(url2).netloc
            response = requests.get(url2, headers=headers, timeout=10).text

            iframes = re.findall(r'iframe src="([^"]*)', response)
            if not iframes:
                print("No iframe src found in url2 response.")
                return None, None

            url3 = iframes[0]
            if not url3.startswith('http'):
                url3 = f"https://{urlparse(headers['Referer']).netloc}{url3}"

            headers['Referer'] = url3
            headers['Origin'] = urlparse(url3).scheme + "://" + urlparse(url3).netloc
            response = requests.get(url3, headers=headers, timeout=10).text

            # --- CRITICAL FIXES FOR BASE64 DECODING AND REGEXES ---
            # channelKey is NOT base64 encoded based on addon.py's parsing
            channel_key_match = re.findall(r'channelKey = \"([^"]*)', response)
            channel_key = channel_key_match[0] if channel_key_match else None

            # These are extracted from atob() calls, so they ARE base64 encoded
            auth_ts_b64 = re.findall(r'c = atob\("([^"]*)', response)
            auth_rnd_b64 = re.findall(r'd = atob\("([^"]*)', response)
            auth_sig_b64 = re.findall(r'e = atob\("([^"]*)', response)
            auth_host_b64 = re.findall(r'a = atob\("([^"]*)', response) # Corrected regex for auth_host
            auth_php_b64 = re.findall(r'b = atob\("([^"]*)', response)

            if not all([channel_key, auth_ts_b64, auth_rnd_b64, auth_sig_b64, auth_host_b64, auth_php_b64]):
                print("Failed to extract all authentication parameters (some might be base64).")
                return None, None

            # Decode the base64 encoded parameters
            auth_ts = base64.b64decode(auth_ts_b64[0]).decode('utf-8') if auth_ts_b64 else None
            auth_rnd = base64.b64decode(auth_rnd_b64[0]).decode('utf-8') if auth_rnd_b64 else None
            # auth_sig needs to be decoded first, then URL quoted
            auth_sig_decoded = base64.b64decode(auth_sig_b64[0]).decode('utf-8') if auth_sig_b64 else None
            auth_sig = quote_plus(auth_sig_decoded) if auth_sig_decoded else None

            auth_host = base64.b64decode(auth_host_b64[0]).decode('utf-8') if auth_host_b64 else None
            auth_php = base64.b64decode(auth_php_b64[0]).decode('utf-8') if auth_php_b64 else None

            if not all([channel_key, auth_ts, auth_rnd, auth_sig, auth_host, auth_php]):
                print("Failed to decode all authentication parameters after base64 processing.")
                return None, None

            auth_url = f'{auth_host}{auth_php}?channel_id={channel_key}&ts={auth_ts}&rnd={auth_rnd}&sig={auth_sig}'
            requests.get(auth_url, headers=headers, timeout=10)

            # Continue with existing logic for host and server_lookup
            host_match = re.findall('(?s)m3u8 =.*?:.*?:.*?\".*?\".*?\"([^\"]*)', response)
            if not host_match:
                print("Could not find m3u8 host in response.")
                return None, None
            host = host_match[0]

            server_lookup_match = re.findall('n fetchWithRetry\\(\\s*\'([^\']*)', response)
            if not server_lookup_match:
                print("Could not find server lookup URL in response.")
                return None, None
            server_lookup = server_lookup_match[0]

            server_lookup_url = f"https://{urlparse(url3).netloc}{server_lookup}{channel_key}"
            server_response = requests.get(server_lookup_url, headers=headers, timeout=10).json()
            server_key = server_response.get('server_key')

            if not server_key:
                print("Could not get server_key from server lookup.")
                return None, None

            final_hls_url = f'https://{server_key}{host}{server_key}/{channel_key}/mono.m3u8'

            hls_headers = {
                'Referer': f"https://{urlparse(url3).netloc}/",
                'Origin': f"https://{urlparse(url3).netloc}",
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