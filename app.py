from flask import Flask, Response, request, stream_with_context, abort
# NOTE: daddylive_api is still imported but its live_tv logic is replaced by DB.
from daddylive_api import daddylive_api # Assuming this is the instantiated object
import re
import os
import sqlite3
import time
import html
from urllib.parse import urlparse, urljoin, quote, unquote_plus
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from collections import defaultdict

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

app = Flask(__name__)

# --- Configuration ---
DL_CONFIG_DB = 'DLConfig.db'
UPDATE_INTERVAL_HOURS = 12

# Configure a session with retry logic
session = requests.Session()
retries = Retry(total=5,
                backoff_factor=0.5,
                status_forcelist=[500, 502, 503, 504],
                allowed_methods=frozenset(['GET', 'POST']))
session.mount('http://', HTTPAdapter(max_retries=retries))
session.mount('https://', HTTPAdapter(max_retries=retries))
session.verify = False

# --- Database Helpers (Task 1) ---

def get_db_connection():
    """Returns a new SQLite connection."""
    conn = sqlite3.connect(DL_CONFIG_DB)
    conn.row_factory = sqlite3.Row # Allows accessing columns by name
    return conn

def init_db():
    """Ensures the LiveTV table exists (if not already set up by user)."""
    conn = get_db_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS "LiveTV" ( 
                "DLChNo"	INTEGER UNIQUE, 
                "DLChName"	TEXT, 
                "OutputChNo"	INTEGER, 
                "OutputChName"	TEXT, 
                "GracenoteID"	TEXT, 
                "XMLGuideSource"	TEXT, 
                "XMLChID"	TEXT, 
                "ChLogoURL"	TEXT, 
                "OutputM3UFile"	TEXT 
            );
        """)
        conn.commit()
    except Exception as e:
        print(f"DB initialization error: {e}")
    finally:
        conn.close()

# --- DLLinks Logic Extraction for Update (Task 2) ---

class ChannelNameUpdater:
    """Helper class to encapsulate logic from DLLinks.py for name fetching."""
    
    def __init__(self):
        self.UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36'
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({'User-Agent': self.UA, 'Connection': 'Keep-Alive'})
        self.baseurl = 'https://daddylivestream.com' # Fallback/Direct use of the common URL
        self._initialize_base_url()

    def _initialize_base_url(self):
        """Fetches the current base URL, mimicking the logic in DLLinks.py."""
        try:
            main_url_content = self.session.get(
                'https://raw.githubusercontent.com/thecrewwh/dl_url/refs/heads/main/dl.xml',
                timeout=5
            ).text
            found_iframe_src = re.findall('src = "([^"]*)', main_url_content)
            if found_iframe_src:
                iframe_url = found_iframe_src[0]
                parsed_iframe_url = urlparse(iframe_url)
                self.baseurl = f"{parsed_iframe_url.scheme}://{parsed_iframe_url.netloc}"
            print(f"Updater Base URL: {self.baseurl}")
        except Exception as e:
            print(f"Error initializing base URL, using default: {e}")

    def get_headers(self):
        """Generate headers for requests."""
        return {
            'User-Agent': self.UA,
            'Connection': 'Keep-Alive',
            'Referer': f'{self.baseurl}/',
            'Origin': self.baseurl
        }

    def extract_all_streams(self):
        """Extracts all streams' IDs and names from the 24-7-channels page."""
        url = f'{self.baseurl}/24-7-channels.php'
        headers = self.get_headers()
        
        try:
            resp = self.session.get(url, headers=headers, timeout=10).text
            channel_items = re.findall(
                r'href="/stream/stream-(\d+)\.php"[^>]*>\s*(?:<[^>]+>)*([^<]+)',
                resp,
                re.DOTALL
            )
            
            channels_by_id = {}
            for channel_id, name in channel_items:
                clean_name = re.sub(r'[\s\n\t]+', ' ', html.unescape(name.strip())).strip()
                if channel_id not in channels_by_id:
                    channels_by_id[int(channel_id)] = clean_name
            
            # Convert to list of dicts: [{'DLChNo': int, 'DLChName': str}]
            results = [{'DLChNo': ch_id, 'DLChName': ch_name} for ch_id, ch_name in channels_by_id.items()]
            return results
            
        except Exception as e:
            print(f"Error fetching streams for update: {e}")
            return []

updater = ChannelNameUpdater()

def update_dl_channel_names():
    """Scheduled task to update DLChName in the database."""
    print(f"Starting scheduled channel name update at {datetime.now()}")
    new_channels = updater.extract_all_streams()
    
    if not new_channels:
        print("Update failed: No channels extracted.")
        return

    conn = get_db_connection()
    try:
        # Use a transaction for efficiency
        conn.execute("BEGIN TRANSACTION")
        
        # 1. Update existing channels' DLChName
        for ch in new_channels:
            conn.execute(
                "UPDATE LiveTV SET DLChName = ? WHERE DLChNo = ?",
                (ch['DLChName'], ch['DLChNo'])
            )
            
        # 2. Insert new channels with default values
        for ch in new_channels:
            conn.execute(
                """INSERT OR IGNORE INTO LiveTV (DLChNo, DLChName, OutputM3UFile) 
                   VALUES (?, ?, ?)""",
                (ch['DLChNo'], ch['DLChName'], 'live_tv.m3u')
            )
        
        conn.commit()
        print(f"Scheduled update complete. {len(new_channels)} channels processed.")
    except Exception as e:
        conn.rollback()
        print(f"Database update failed: {e}")
    finally:
        conn.close()

# --- M3U Generation from DB (Task 3) ---

@app.route('/daddylive/live_tv_m3u/<m3u_filename>')
def generate_dynamic_m3u(m3u_filename):
    """
    Generates M3U content for a specific file name from the DB.
    """
    conn = get_db_connection()
    try:
        channels = conn.execute(
            'SELECT * FROM LiveTV WHERE OutputM3UFile = ? ORDER BY OutputChNo ASC', 
            (m3u_filename,)
        ).fetchall()
    except Exception as e:
        print(f"Error querying DB for M3U: {e}")
        return Response("#EXTM3U\n# Database Error.", mimetype="audio/x-mpegurl")
    finally:
        conn.close()

    if not channels:
        return Response(f"#EXTM3U\n# No live TV channels found for file: {m3u_filename}", mimetype="audio/x-mpegurl")

    m3u_content = [f"#EXTM3U name=\"{m3u_filename.replace('.m3u','')}\""]
    
    for ch in channels:
        proxy_url = f"{request.url_root.rstrip('/')}/daddylive/hls/{ch['DLChNo']}/{quote('mono.m3u8', safe='')}"
        
        extinf_line = f"""#EXTINF:-1 """
        
        if ch['XMLChID']:
             extinf_line += f"""channel-id="{ch['XMLChID']}" tvg-name="{ch['XMLChID']}" """
        
        if ch['OutputChNo']:
             extinf_line += f"""channel-number="{ch['OutputChNo']}" """
        
        if ch['ChLogoURL']:
             extinf_line += f"""tvg-logo="{ch['ChLogoURL']}" """
             
        if ch['GracenoteID']:
             extinf_line += f"""tvc-guide-stationid="{ch['GracenoteID']}" """
        
        extinf_line += f"""group-title="{ch['OutputChName'] or 'Live TV'}",{ch['OutputChName'] or ch['DLChName']}"""
        
        m3u_content.append(extinf_line)
        m3u_content.append(proxy_url)
        
    return Response("\n".join(m3u_content), mimetype="audio/x-mpegurl")


# --- HLS Stream Proxy (STABILITY FIX APPLIED) ---
@app.route('/daddylive/hls/<channel_id>/<path:proxied_path>')
def hls_proxy(channel_id, proxied_path):
    original_requested_resource = unquote_plus(proxied_path)
    print(f"\n[HLS PROXY] === Processing request for channel {channel_id} ===")
    
    # Attempt 1: Standard stream resolution
    original_hls_manifest_url, headers_for_upstream = daddylive_api.resolve_stream(channel_id)

    # **STABILITY FIX:** If resolution fails, assume the upstream API is stale and reset the state
    if not original_hls_manifest_url:
        print(f"Stream resolution failed for {channel_id}. Attempting API reset...")
        
        try:
            # 1. Clear the stream cache (assuming cache_lock is available on daddylive_api instance)
            with daddylive_api.cache_lock:
                daddylive_api.stream_cache = {}
            
            # 2. Force re-initialization of the base URLs (assuming _initialize_base_urls is available)
            daddylive_api._initialize_base_urls()

            # Attempt 2: Retry stream resolution
            original_hls_manifest_url, headers_for_upstream = daddylive_api.resolve_stream(channel_id)
        except Exception as e:
            print(f"API reset failed with error: {e}")
            pass # Continue to final failure check

        if not original_hls_manifest_url:
            print(f"Stream resolution failed again for {channel_id} after reset.")
            abort(500, "Could not resolve HLS stream from upstream after reset.")
        else:
            print(f"Stream resolution successful for {channel_id} after reset.")
            
    # Continue processing the stream...
    parsed_url = urlparse(original_hls_manifest_url)
    upstream_base = f"{parsed_url.scheme}://{parsed_url.netloc}{os.path.dirname(parsed_url.path).rstrip('/')}/"
    proxy_content = original_requested_resource.endswith('.m3u8')

    if proxy_content:
        upstream_file_url = original_hls_manifest_url
    elif original_requested_resource.startswith(('http://','https://')):
        upstream_file_url = original_requested_resource
    else:
        upstream_file_url = urljoin(upstream_base, original_requested_resource)

    try:
        upstream_response = session.get(upstream_file_url, headers=headers_for_upstream, stream=True, timeout=(5,30))
        upstream_response.raise_for_status()
        mimetype = upstream_response.headers.get('Content-Type','application/octet-stream')
        if original_requested_resource.endswith('.m3u8'):
            mimetype = 'application/x-mpegURL'
        elif original_requested_resource.endswith('.ts'):
            mimetype = 'video/mp2t'

        if proxy_content:
            content = upstream_response.text
            rewritten_lines = []
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith('#EXT-X-KEY'):
                    key_match = re.search(r'#EXT-X-KEY:METHOD=(.+?),URI="([^"]+)"', stripped)
                    if key_match:
                        method = key_match.group(1)
                        key_uri = key_match.group(2)
                        resolved_key = urljoin(upstream_file_url, key_uri)
                        proxied_key = f'/daddylive/hls/{channel_id}/{quote(resolved_key,safe="")}'
                        rewritten_lines.append(f'#EXT-X-KEY:METHOD={method},URI="{proxied_key}"')
                        continue
                if stripped and not stripped.startswith('#'):
                    resolved_media = urljoin(upstream_file_url, stripped)
                    proxied_media = f'/daddylive/hls/{channel_id}/{quote(resolved_media,safe="")}'
                    rewritten_lines.append(proxied_media)
                else:
                    rewritten_lines.append(line)
            return Response("\n".join(rewritten_lines), mimetype=mimetype)
        else:
            return Response(stream_with_context(upstream_response.iter_content(chunk_size=8192)), mimetype=mimetype)
    except Exception as e:
        import traceback
        traceback.print_exc()
        abort(500, description=str(e))

# --- Events M3U (UNCHANGED) ---
@app.route('/daddylive/events.m3u')
def generate_events_m3u():
    return generate_events_m3u_part(1)

@app.route('/daddylive/events_part<int:part>.m3u')
def generate_events_m3u_part(part):
    events_data = daddylive_api.get_scheduled_events()
    if not events_data:
        return Response("#EXTM3U\n# No scheduled events found.", mimetype="audio/x-mpegurl")

    all_entries = []
    for category, events_list in events_data.items():
        if category=="TV Shows": continue
        for event in events_list:
            for ch in event['channels']:
                if not ch['id']: continue
                tvg_id = ch['id']
                full_name = f"{event['title']} ({ch['name']})"
                proxy_url = f"{request.url_root.rstrip('/')}/daddylive/hls/{ch['id']}/{quote('mono.m3u8', safe='')}"
                all_entries.append({'tvg_id':tvg_id,'full_name':full_name,'category':category,'proxy_url':proxy_url})

    MAX_STREAMS_PER_FILE = 750
    total_entries = len(all_entries)
    total_parts = (total_entries + MAX_STREAMS_PER_FILE - 1)//MAX_STREAMS_PER_FILE
    if part<1 or part>total_parts:
        return Response(f"#EXTM3U\n# Invalid part number. Available: 1-{total_parts}", mimetype="audio/x-mpegurl")

    start_idx = (part-1)*MAX_STREAMS_PER_FILE
    end_idx = min(start_idx + MAX_STREAMS_PER_FILE, total_entries)
    subset = all_entries[start_idx:end_idx]

    m3u_content = ["#EXTM3U", f"# Events Part {part}/{total_parts}"]
    for entry in subset:
        m3u_content.append(f"#EXTINF:-1 tvg-id=\"{entry['tvg_id']}\" tvg-name=\"{entry['full_name']}\" group-title=\"Events - {entry['category']}\",{entry['full_name']}")
        m3u_content.append(entry['proxy_url'])
    return Response("\n".join(m3u_content), mimetype="audio/x-mpegurl")

# --- XMLTV Guide (UNCHANGED) ---
@app.route('/daddylive/guide.xml')
def generate_xmltv_from_m3u():
    import requests
    from xml.etree.ElementTree import Element, SubElement, tostring
    from xml.dom import minidom
    from datetime import datetime, timedelta, timezone

    m3u_url = f"{request.url_root.rstrip('/')}/daddylive/events.m3u"
    r = requests.get(m3u_url, verify=False)
    if r.status_code != 200:
        return "Failed to fetch M3U", 500

    lines = r.text.splitlines()
    tv = Element('tv')
    added_channels = {}

    for i, line in enumerate(lines):
        if line.startswith('#EXTINF:'):
            tvg_id_match = re.search(r'tvg-id="([^"]+)"', line)
            tvg_name_match = re.search(r'tvg-name="([^"]+)"', line)
            if tvg_id_match and tvg_name_match:
                tvg_id = tvg_id_match.group(1)
                tvg_name = tvg_name_match.group(1)
                if tvg_id not in added_channels:
                    ch_elem = SubElement(tv, 'channel', id=tvg_id)
                    display_name = SubElement(ch_elem, 'display-name')
                    display_name.text = tvg_name
                    added_channels[tvg_id] = tvg_name

    today = datetime.now(timezone.utc).date()
    start_dt = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(days=1)

    for tvg_id, tvg_name in added_channels.items():
        prog_elem = SubElement(tv, 'programme',
                               start=start_dt.strftime('%Y%m%d%H%M%S +0000'),
                               stop=end_dt.strftime('%Y%m%d%H%M%S +0000'),
                               channel=tvg_id)
        title = SubElement(prog_elem, 'title')
        title.text = tvg_name
        category_elem = SubElement(prog_elem, 'category')
        category_elem.text = "Live Sports"

    xml_str = minidom.parseString(tostring(tv)).toprettyxml(indent="  ")
    return Response(xml_str, mimetype='application/xml')


# --- Force Refresh Endpoint ---
@app.route('/daddylive/refresh_names', methods=['POST'])
def force_refresh_names():
    """Endpoint to manually trigger channel name update."""
    update_dl_channel_names()
    return Response("Channel names refreshed successfully.", mimetype="text/plain")

# --- Index ---
@app.route('/')
def index():
    base = request.url_root.rstrip('/')
    
    # Get all unique OutputM3UFile entries for the index page
    conn = get_db_connection()
    try:
        m3u_files = conn.execute(
            'SELECT DISTINCT OutputM3UFile FROM LiveTV WHERE OutputM3UFile IS NOT NULL'
        ).fetchall()
        
        # Also count total channels for the summary
        total_channels = conn.execute('SELECT COUNT(DLChNo) FROM LiveTV').fetchone()[0]
    except Exception as e:
        print(f"Error querying DB for index: {e}")
        m3u_files = []
        total_channels = 0
    finally:
        conn.close()

    live_links = [
        f'<li><a href="{base}/daddylive/live_tv_m3u/{m3u["OutputM3UFile"]}">{m3u["OutputM3UFile"]}</a></li>' 
        for m3u in m3u_files
    ]
    
    events_data = daddylive_api.get_scheduled_events()
    total_events = sum(len([ch for e in lst for ch in e['channels'] if ch['id']]) 
                       for cat,lst in events_data.items() if cat!="TV Shows")
    MAX_STREAMS_PER_FILE = 750
    event_parts = (total_events + MAX_STREAMS_PER_FILE -1)//MAX_STREAMS_PER_FILE
    events_links = [f'<li><a href="{base}/daddylive/events{"_part"+str(i) if i>1 else ""}.m3u">Events Part {i}</a></li>'
                    for i in range(1,event_parts+1)]

    return f"""
    <html><head><title>DaddyLive Proxy</title></head><body>
    <h1>DaddyLive Proxy for Channels DVR</h1>
    <p>Live TV: {total_channels} channels ({len(m3u_files)} files)</p>
    <ul>{''.join(live_links)}</ul>
    <p>Events: {total_events} channels ({event_parts} files)</p>
    <ul>{''.join(events_links)}</ul>
    <p>XMLTV Guide (Live Events only): <a href="{base}/daddylive/guide.xml">Download Guide</a></p>
    <hr>
    <form method="POST" action="{base}/daddylive/refresh_names" onsubmit="this.querySelector('button').disabled=true; this.querySelector('button').innerText='Refreshing...';">
        <button type="submit">Force Channel Name Refresh</button>
    </form>
    </body></html>
    """

# --- App Execution and Scheduling ---

# Initialize the database and scheduler
init_db()
scheduler = BackgroundScheduler()
scheduler.add_job(
    func=update_dl_channel_names, 
    trigger='interval', 
    hours=UPDATE_INTERVAL_HOURS, 
    id='dl_name_updater',
    name='Update DL Channel Names',
    max_instances=1
)
scheduler.start()
print(f"Scheduler started: DL Channel names will update every {UPDATE_INTERVAL_HOURS} hours.")


if __name__=='__main__':
    print("Performing initial channel name update...")
    update_dl_channel_names() 
    print("Initial update complete. Starting Flask app.")

    host = os.environ.get('HOST','0.0.0.0')
    port = int(os.environ.get('PORT',5000))
    app.run(host=host, port=port, debug=False, use_reloader=False)