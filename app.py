from flask import Flask, Response, request, stream_with_context, abort
from daddylive_api import daddylive_api
import re
import os
import sys
from urllib.parse import urlparse, urljoin, quote, unquote_plus
import requests
import time
from requests.adapters import HTTPAdapter # ADDED: Import HTTPAdapter
from urllib3.util.retry import Retry # ADDED: Import Retry

app = Flask(__name__)

# Configure a session with retry logic for network issues
# ADDED: Start of new session configuration
session = requests.Session()
retries = Retry(total=5,  # Number of retries
                backoff_factor=0.5, # Backoff for retries
                status_forcelist=[500, 502, 503, 504], # Retry on these HTTP statuses
                allowed_methods=frozenset(['GET', 'POST'])) # Methods to retry
session.mount('http://', HTTPAdapter(max_retries=retries))
session.mount('https://', HTTPAdapter(max_retries=retries))
# ADDED: End of new session configuration


# --- Helper for HLS Stream Proxying ---

@app.route('/daddylive/hls/<channel_id>/<path:proxied_path>')
def hls_proxy(channel_id, proxied_path):
    """
    Proxies HLS manifest (.m3u8) and segment (.ts) files, rewriting URLs within manifests.
    `proxied_path` is the URL-encoded original path/URL of the resource we need to fetch from upstream.
    """
    print(f"[App Proxy] Received request for channel_id: {channel_id}, proxied_path: {proxied_path}")

    # Decode the proxied_path to get the original resource path/URL that ffplay/DVR requested
    original_requested_resource = unquote_plus(proxied_path)

    # Resolve the actual HLS manifest URL and required headers from DaddyLiveAPI
    # This ensures we get the most up-to-date base URL and headers for the upstream server.
    original_hls_manifest_url, headers_for_upstream = daddylive_api.resolve_stream(channel_id)

    if not original_hls_manifest_url:
        print(f"[App Proxy] Failed to resolve HLS stream URL for channel_id: {channel_id}")
        abort(500, "Could not resolve HLS stream from upstream.")

    # Determine the base URL of the *original* HLS stream from the upstream server.
    # This is used to resolve relative paths found within the manifest itself.
    parsed_original_manifest_url = urlparse(original_hls_manifest_url)
    upstream_hls_base_url = f"{parsed_original_manifest_url.scheme}://{parsed_original_manifest_url.netloc}{os.path.dirname(parsed_original_manifest_url.path).rstrip('/')}/"

    # Determine the actual URL to fetch from the upstream server and if content needs rewriting
    proxy_content = original_requested_resource.endswith('.m3u8') # Simplify: Rewrite if it's any .m3u8 file

    if proxy_content:
        # If it's a manifest, always fetch the one identified by resolve_stream
        upstream_file_url = original_hls_manifest_url
        print(f"[App Proxy] Request for manifest: {upstream_file_url}")
    elif original_requested_resource.startswith('http://') or original_requested_resource.startswith('https://'):
        # If the decoded proxied_path is already an absolute URL (like a key or segment URL from the original manifest)
        upstream_file_url = original_requested_resource
        print(f"[App Proxy] Request for absolute URL: {upstream_file_url}")
    else:
        # If it's a relative path (e.g., segment.ts, or a sub-playlist from a parent manifest)
        upstream_file_url = urljoin(upstream_hls_base_url, original_requested_resource)
        print(f"[App Proxy] Request for relative path: {upstream_file_url}")


    print(f"[App Proxy] Fetching from upstream: {upstream_file_url}")
    print(f"[App Proxy] Upstream Headers: {headers_for_upstream}")

    try:
        start_fetch_time = time.time() # Start timer
        # MODIFIED: Use the 'session' object with retry logic
        upstream_response = session.get(upstream_file_url, headers=headers_for_upstream, stream=True, timeout=(5, 30)) # Connect timeout 5s, Read timeout 30s
        upstream_response.raise_for_status() # Raise an exception for HTTP errors (4xx or 5xx)
        end_fetch_time = time.time() # End timer
        print(f"[App Proxy] Fetched {original_requested_resource} from upstream in {end_fetch_time - start_fetch_time:.2f} seconds.")

        # Set correct Content-Type for the response
        mimetype = upstream_response.headers.get('Content-Type', 'application/octet-stream')
        if original_requested_resource.endswith('.m3u8'):
            mimetype = 'application/x-mpegURL'
        elif original_requested_resource.endswith('.ts'):
            mimetype = 'video/mp2t'

        # Rewrite M3U8 content to point all internal URLs back to our proxy
        if proxy_content: # This will now correctly be True for M3U8 files
            content = upstream_response.text

            rewritten_lines = []
            for line in content.splitlines():
                stripped_line = line.strip()
                if not stripped_line:
                    rewritten_lines.append(line)
                    continue

                if stripped_line.startswith('#EXT'):
                    # Check for URI in EXT-X-KEY
                    key_match = re.search(r'#EXT-X-KEY:METHOD=(.+?),URI="([^"]+)"', stripped_line)
                    if key_match:
                        method = key_match.group(1)
                        original_key_uri = key_match.group(2)
                        
                        resolved_key_uri = urljoin(upstream_file_url, original_key_uri)
                        proxied_key_uri = f'/daddylive/hls/{channel_id}/{quote(resolved_key_uri, safe="")}'
                        rewritten_lines.append(f'#EXT-X-KEY:METHOD={method},URI="{proxied_key_uri}"')
                    else:
                        rewritten_lines.append(line) # Keep other EXT lines as is
                elif not stripped_line.startswith('#'):
                    # Assume it's a media segment URL or a sub-playlist URL
                    original_media_url = stripped_line
                    
                    resolved_media_url = urljoin(upstream_file_url, original_media_url)
                    proxied_media_url = f'/daddylive/hls/{channel_id}/{quote(resolved_media_url, safe="")}'
                    rewritten_lines.append(proxied_media_url)
                else:
                    rewritten_lines.append(line) # Keep comments as is
            
            rewritten_content = "\n".join(rewritten_lines)
            print(f"[App Proxy] Rewrote M3U8 for {channel_id}. Manifest size: {len(rewritten_content)} chars.")
            return Response(rewritten_content, mimetype=mimetype)
        else:
            # For segments or other binary files, stream directly
            print(f"[App Proxy] Streaming binary data for {original_requested_resource} from upstream.")
            return Response(stream_with_context(upstream_response.iter_content(chunk_size=8192)), mimetype=mimetype)

    except requests.exceptions.RequestException as e:
        print(f"[App Proxy] Error fetching upstream HLS content for {original_requested_resource}: {e}")
        import traceback
        traceback.print_exc()
        abort(500, description=f"Failed to proxy HLS stream: {e}")
    except Exception as e:
        print(f"[App Proxy] Unexpected error during HLS proxy for {original_requested_resource}: {e}")
        import traceback
        traceback.print_exc()
        abort(500, description=f"An unexpected error occurred during proxying: {e}")

# --- M3U Generation Endpoints (no changes needed here) ---

@app.route('/daddylive/live_tv.m3u')
def generate_live_tv_m3u():
    """Generates an M3U playlist for 24/7 live TV channels."""
    channels = daddylive_api.get_all_streams()
    if not channels:
        return Response("#EXTM3U\n# No live TV channels found.", mimetype="audio/x-mpegurl")

    m3u_content = ["#EXTM3U"]
    for ch in channels:
        proxy_url = f"{request.url_root.rstrip('/')}/daddylive/hls/{ch['id']}/{quote('mono.m3u8', safe='')}"
        m3u_content.append(f"#EXTINF:-1 tvg-id=\"{ch['id']}\" tvg-name=\"{ch['name']}\" group-title=\"Live TV\",{ch['name']}")
        m3u_content.append(proxy_url)

    print(f"[App M3U] Generated Live TV M3U with {len(channels)} channels.")
    return Response("\n".join(m3u_content), mimetype="audio/x-mpegurl")


@app.route('/daddylive/events.m3u')
def generate_events_m3u():
    """
    Generates an M3U playlist for scheduled events.
    This will include all available event streams, grouped by category and then event title.
    Note: This can result in a very large M3U.
    """
    events_data = daddylive_api.get_scheduled_events()
    if not events_data:
        return Response("#EXTM3U\n# No scheduled events found.", mimetype="audio/x-mpegurl")

    m3u_content = ["#EXTM3U"]
    total_events_added = 0
    for category, events_list in events_data.items():
        for event in events_list:
            event_title = event['title']
            for ch in event['channels']:
                channel_name = ch['name']
                channel_id = ch['id']
                if channel_id:
                    proxy_url = f"{request.url_root.rstrip('/')}/daddylive/hls/{channel_id}/{quote('mono.m3u8', safe='')}"
                    tvg_id = f"{channel_id}-{re.sub(r'[^a-zA-Z0-9]', '', event_title).lower()}"
                    full_name = f"{event_title} ({channel_name})"
                    m3u_content.append(f"#EXTINF:-1 tvg-id=\"{tvg_id}\" tvg-name=\"{full_name}\" group-title=\"Events - {category}\",{full_name}")
                    m3u_content.append(proxy_url)
                    total_events_added += 1

    print(f"[App M3U] Generated Events M3U with {total_events_added} total event channels.")
    return Response("\n".join(m3u_content), mimetype="audio/x-mpegurl")


@app.route('/')
def index():
    """Provides a simple index page with links to the M3U files."""
    base_url = request.url_root.rstrip('/')
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>DaddyLive Proxy for Channels DVR</title>
        <style>
            body {{ font-family: sans-serif; margin: 40px; background-color: #f4f4f4; color: #333; }}
            h1 {{ color: #0056b3; }}
            ul {{ list-style-type: none; padding: 0; }}
            li {{ margin-bottom: 10px; }}
            a {{ color: #007bff; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            .note {{ background-color: #fff3cd; border-left: 5px solid #ffeeba; padding: 10px; margin-top: 20px; }}
        </style>
    </head>
    <body>
        <h1>DaddyLive Proxy for Channels DVR</h1>
        <p>Use these URLs in your Channels DVR custom channel setup:</p>
        <ul>
            <li><strong>24/7 Live TV Channels M3U:</strong> <a href="{base_url}/daddylive/live_tv.m3u">{base_url}/daddylive/live_tv.m3u</a></li>
            <li><strong>Scheduled Events M3U (Can be very large):</strong> <a href="{base_url}/daddylive/events.m3u">{base_url}/daddylive/events.m3u</a></li>
        </ul>
        <div class="note">
            <p>
                <strong>Note:</strong> This server acts as a proxy for the DaddyLive streams.
                It must be running and accessible by your Channels DVR server for streams to work.
                The Scheduled Events M3U can be very large and might include many channels
                that are not currently active, potentially causing performance issues or clutter in Channels DVR.
                The 24/7 Live TV M3U is generally recommended for ongoing use.
            </p>
            <p>
                If streams stop working, check the console where this Python script is running for errors.
                Website structures can change, requiring updates to the parsing logic.
            </p>
        </div>
    </body>
    </html>
    """

if __name__ == '__main__':
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', 5000))
    app.run(host=host, port=port, debug=False, use_reloader=False)