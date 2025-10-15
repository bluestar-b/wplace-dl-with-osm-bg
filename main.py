import math
import requests
from PIL import Image
from io import BytesIO
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import argparse
from datetime import datetime

def lat_lon_to_tile(lat, lon, zoom):
    n = 2 ** zoom
    x = (lon + 180) / 360 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return x, y

def get_tiles_in_bbox(min_lat, max_lat, min_lon, max_lon, zoom):
    if min_lat > max_lat:
        min_lat, max_lat = max_lat, min_lat
    if min_lon > max_lon:
        min_lon, max_lon = max_lon, min_lon

    coords = [
        lat_lon_to_tile(max_lat, min_lon, zoom),  # NW
        lat_lon_to_tile(max_lat, max_lon, zoom),  # NE
        lat_lon_to_tile(min_lat, min_lon, zoom),  # SW
        lat_lon_to_tile(min_lat, max_lon, zoom),  # SE
    ]
    x_vals = [c[0] for c in coords]
    y_vals = [c[1] for c in coords]

    start_x = int(math.floor(min(x_vals)))
    end_x = int(math.ceil(max(x_vals)))
    start_y = int(math.floor(min(y_vals)))
    end_y = int(math.ceil(max(y_vals)))

    n = 2 ** zoom
    start_x = max(0, min(start_x, n - 1))
    end_x = max(0, min(end_x, n - 1))
    start_y = max(0, min(start_y, n - 1))
    end_y = max(0, min(end_y, n - 1))

    return [(x, y) for x in range(start_x, end_x + 1) for y in range(start_y, end_y + 1)]

def get_tile_image(url, cache_path, headers=None, timeout=10, delay=5):
    cache_path = Path(cache_path)
    if cache_path.exists():
        print(f"cache hit: {cache_path}")
        return Image.open(cache_path)

    while True:
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with open(cache_path, 'wb') as f:
                    f.write(resp.content)
                print(f"downloaded: {url} -> {cache_path}")
                return Image.open(BytesIO(resp.content))
            elif resp.status_code == 429:
                print(f"http 429 for {url}. waiting {delay} seconds.")
                time.sleep(delay)
            else:
                print(f"http {resp.status_code} for {url}, retrying in {delay} seconds...")
                time.sleep(delay)
        except requests.exceptions.RequestException as e:
            print(f"error fetching {url}: {e}, retrying in {delay} seconds...")
            time.sleep(delay)

def get_tile_image_no_cache(url, headers=None, timeout=10, delay=5):
    """Downloads a tile from a URL without caching."""
    while True:
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                print(f"downloaded: {url}")
                return Image.open(BytesIO(resp.content))
            elif resp.status_code == 429:
                print(f"http 429 for {url}. waiting {delay} seconds.")
                time.sleep(delay)
            else:
                print(f"http {resp.status_code} for {url}, retrying in {delay} seconds...")
                time.sleep(delay)
        except requests.exceptions.RequestException as e:
            print(f"error fetching {url}: {e}, retrying in {delay} seconds...")
            time.sleep(delay)

def process_and_composite_tile(tile_info):
    x, y, zoom, TILE_SIZE, CACHE_DIR, OSM_URL, WPLACE_URL = tile_info

    #osm bg
    osm_cache_path = Path(CACHE_DIR) / str(zoom) / "osm" / str(x) / f"{y}.png"
    osm_url = OSM_URL.format(z=zoom, x=x, y=y)
    bg = get_tile_image(osm_url, osm_cache_path, headers={'User-Agent': 'meow meow/1.0'})
    if bg:
        bg = bg.convert('RGB').resize((TILE_SIZE, TILE_SIZE), Image.LANCZOS)
    else:
        bg = Image.new('RGB', (TILE_SIZE, TILE_SIZE), (220, 220, 220))


    #wp overlay
    wp_url = WPLACE_URL.format(x=x, y=y)
    overlay = get_tile_image_no_cache(wp_url, timeout=15)

    custom_tile_used = False
    if overlay:
        if overlay.mode in ('RGBA', 'LA') or (overlay.mode == 'P' and 'transparency' in overlay.info):
            overlay = overlay.convert('RGBA')
            custom_tile_used = True
        else:
            overlay = overlay.convert('RGBA')

        if overlay.size != (TILE_SIZE, TILE_SIZE):
            overlay = overlay.resize((TILE_SIZE, TILE_SIZE), Image.LANCZOS)
        bg.paste(overlay, (0, 0), overlay)

    return (x, y, bg, custom_tile_used)


min_lat = 13.870763044489482    # South


max_lat = 13.775688622735316  # North


min_lon = 99.06565396552733   # West


max_lon =102.76180630927732  # East



zoom = 11

OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
WPLACE_URL = "https://backend.wplace.live/files/s0/tiles/{x}/{y}.png"

TILE_SIZE = 1000
CACHE_DIR = "tile_cache"
MAX_WORKERS = 10


parser = argparse.ArgumentParser(description='Stitch map tiles.')
parser.add_argument('target_name', help='Target name for the output file.')
args = parser.parse_args()

tiles = get_tiles_in_bbox(min_lat, max_lat, min_lon, max_lon, zoom)
print(f"total tiles to process: {len(tiles)}")

if not tiles:
    raise SystemExit("no tiles found!")

x_vals = [t[0] for t in tiles]
y_vals = [t[1] for t in tiles]
min_x, max_x = min(x_vals), max(x_vals)
min_y, max_y = min(y_vals), max(y_vals)

cols = max_x - min_x + 1
rows = max_y - min_y + 1
print(f"final image: {cols * TILE_SIZE} x {rows * TILE_SIZE} pixels")

final_image = Image.new('RGB', (cols * TILE_SIZE, rows * TILE_SIZE))
custom_found = 0
processed_count = 0

tasks = [(x, y, zoom, TILE_SIZE, CACHE_DIR, OSM_URL, WPLACE_URL) for x, y in tiles]

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    future_to_tile = {executor.submit(process_and_composite_tile, task): task for task in tasks}
    for future in as_completed(future_to_tile):
        processed_count += 1
        try:
            x, y, processed_tile, custom_used = future.result()
            print(f"[{processed_count}/{len(tiles)}] finished tile ({x}, {y})")
            if custom_used:
                custom_found += 1
            final_image.paste(processed_tile, ((x - min_x) * TILE_SIZE, (y - min_y) * TILE_SIZE))
        except Exception as exc:
            tile_info = future_to_tile[future]
            print(f"error processing tile {tile_info[:2]}: {exc}")


date_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
output = f"{args.target_name}_{date_str}.png"
final_image.save(output)
print(f"done: {custom_found}/{len(tiles)} custom tiles used.")
print(f"saved as {output}")
