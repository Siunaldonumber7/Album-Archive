import re
import time

from cache_utils import BoundedCache
from search_engine import (
    APPLE_SESSION,
    APPLE_PAGE_CACHE,
    normalize_text,
    contains_hangul,
    similarity,
    fetch_apple_music_kr_page,
    get_kr_localized_album_title,
    safe_json_decode,
)


TRACK_CACHE = BoundedCache(500)
TRACK_CACHE_SECONDS = 3600


def clear_track_caches():
    TRACK_CACHE.clear()
    APPLE_PAGE_CACHE.clear()


def has_manual_track_override(conn, album_id):
    if not album_id:
        return False
    row = conn.execute(
        'SELECT manual_override FROM album_track_settings WHERE album_id = ? LIMIT 1',
        (album_id,)
    ).fetchone()
    return bool(row and row['manual_override'])

def get_manual_album_tracks(conn, album_id):
    if not album_id:
        return []
    rows = conn.execute(
        '''
        SELECT disc_number, track_number, title, artist, duration
        FROM album_tracks
        WHERE album_id = ?
        ORDER BY disc_number ASC, track_number ASC, id ASC
        ''',
        (album_id,)
    ).fetchall()
    return [
        {
            'disc_number': int(row['disc_number'] or 1),
            'track_number': int(row['track_number'] or 1),
            'title': row['title'] or '',
            'artist': row['artist'] or '',
            'duration': row['duration'] or ''
        }
        for row in rows
    ]

def normalize_manual_duration(value):
    value = str(value or '').strip()
    if not value:
        return ''

    # 3:45 / 1:03:22 형태만 저장한다.
    parts = value.split(':')
    if len(parts) not in {2, 3} or not all(part.isdigit() for part in parts):
        return ''

    numbers = [int(part) for part in parts]

    if len(numbers) == 2:
        minutes, seconds = numbers
        if seconds >= 60:
            return ''
        return f'{minutes}:{seconds:02d}'

    hours, minutes, seconds = numbers
    if minutes >= 60 or seconds >= 60:
        return ''
    return f'{hours}:{minutes:02d}:{seconds:02d}'

def format_track_duration(milliseconds):
    try:
        milliseconds = int(milliseconds)
    except Exception:
        return ''
    if milliseconds <= 0:
        return ''
    total_seconds = milliseconds // 1000
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f'{minutes}:{seconds:02d}'

def get_artist_from_track_object(value):
    artist = str(value.get('artistName') or value.get('artist_name') or value.get('byline') or '').strip()
    if artist:
        return artist
    subtitle_links = value.get('subtitleLinks')
    if isinstance(subtitle_links, list):
        names = []
        for link in subtitle_links:
            if not isinstance(link, dict):
                continue
            name = str(link.get('title') or '').strip()
            if name:
                names.append(name)
        if names:
            return ', '.join(names)
    subtitle = value.get('subtitle')
    if isinstance(subtitle, str):
        return subtitle.strip()
    return ''

def parse_apple_music_track_object(value):
    if not isinstance(value, dict):
        return None
    object_type = str(value.get('type') or '').lower()
    if object_type in {'music-videos', 'musicvideo', 'musicvideos'}:
        return None
    title = ''
    artist = ''
    album_name = ''
    track_id = ''
    duration_ms = 0
    disc_number = 1
    track_number = 0
    attributes = value.get('attributes')
    if object_type == 'songs' and isinstance(attributes, dict):
        title = str(attributes.get('name') or '').strip()
        artist = str(attributes.get('artistName') or '').strip()
        album_name = str(attributes.get('albumName') or '').strip()
        track_id = str(value.get('id') or '').strip()
        duration_ms = attributes.get('durationInMillis') or 0
        disc_number = attributes.get('discNumber') or 1
        track_number = attributes.get('trackNumber') or 0
    if not title:
        title = str(value.get('songName') or value.get('trackName') or '').strip()
        if not title:
            possible_title = value.get('title')
            if isinstance(possible_title, str):
                title = possible_title.strip()
        if not title and isinstance(value.get('name'), str) and (value.get('artistName') or value.get('subtitleLinks')):
            title = str(value.get('name') or '').strip()
        artist = get_artist_from_track_object(value)
        album_name = str(value.get('albumName') or value.get('collectionName') or '').strip()
        track_id = str(value.get('trackId') or value.get('songId') or value.get('adamId') or '').strip()
        content_descriptor = value.get('contentDescriptor')
        if not track_id and isinstance(content_descriptor, dict):
            identifiers = content_descriptor.get('identifiers')
            if isinstance(identifiers, dict):
                track_id = str(identifiers.get('storeAdamID') or '').strip()
        if not track_id and isinstance(value.get('id'), str):
            match = re.search('(\\d{6,})$', value['id'])
            if match:
                track_id = match.group(1)
        duration_ms = value.get('durationInMillis') or value.get('trackTimeMillis') or value.get('duration') or 0
        disc_number = value.get('discNumber') or 1
        track_number = value.get('trackNumber') or 0
    if not title or len(title) < 1:
        return None
    reject_titles = {'apple music', 'music에서 열기', '미리 듣기', '재생', '전체 보기'}
    if title.lower() in reject_titles:
        return None
    try:
        duration_ms = int(float(duration_ms or 0))
    except Exception:
        duration_ms = 0
    try:
        disc_number = int(disc_number or 1)
    except Exception:
        disc_number = 1
    try:
        track_number = int(track_number or 0)
    except Exception:
        track_number = 0
    has_track_hint = bool(object_type == 'songs' or track_id or track_number or duration_ms or value.get('contentDescriptor') or str(value.get('id') or '').startswith('track-lockup'))
    if not has_track_hint:
        return None
    return {'id': track_id, 'title': title, 'artist': artist, 'album_name': album_name, 'duration_ms': duration_ms, 'duration': format_track_duration(duration_ms), 'disc_number': disc_number, 'track_number': track_number}

def traverse_for_track_objects(root):
    if not isinstance(root, (dict, list)):
        return []
    stack = [root]
    tracks = []
    seen_keys = set()
    safety = 0
    while stack and safety < 200000:
        value = stack.pop()
        safety += 1
        if isinstance(value, dict):
            track = parse_apple_music_track_object(value)
            if track:
                dedupe_key = track['id'] or normalize_text(track['title']) + '|' + normalize_text(track['artist'])
                if dedupe_key and dedupe_key not in seen_keys:
                    seen_keys.add(dedupe_key)
                    tracks.append(track)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    stack.append(child)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, (dict, list)):
                    stack.append(child)
    return tracks

def parse_iso_duration_to_ms(value):
    if not value:
        return 0
    match = re.match('PT(?:(\\d+)H)?(?:(\\d+)M)?(?:(\\d+)S)?', str(value), flags=re.I)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return (hours * 3600 + minutes * 60 + seconds) * 1000

def extract_jsonld_tracks(parser):
    tracks = []
    seen = set()
    for raw in parser.ld_json_scripts:
        data = safe_json_decode(raw)
        if not data:
            continue
        stack = [data]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                type_value = value.get('@type')
                if type_value in {'MusicAlbum', 'MusicPlaylist'}:
                    track_list = value.get('track')
                    if isinstance(track_list, list):
                        for index, item in enumerate(track_list, start=1):
                            if not isinstance(item, dict):
                                continue
                            title = str(item.get('name') or '').strip()
                            if not title:
                                continue
                            artist = ''
                            by_artist = item.get('byArtist')
                            if isinstance(by_artist, dict):
                                artist = str(by_artist.get('name') or '').strip()
                            elif isinstance(by_artist, str):
                                artist = by_artist.strip()
                            duration_ms = parse_iso_duration_to_ms(item.get('duration'))
                            track_url = str(item.get('url') or '')
                            track_id = ''
                            id_match = re.search('/(\\d{6,})(?:\\?|$)', track_url)
                            if id_match:
                                track_id = id_match.group(1)
                            key = track_id or normalize_text(title) + '|' + normalize_text(artist)
                            if key in seen:
                                continue
                            seen.add(key)
                            tracks.append({'id': track_id, 'title': title, 'artist': artist, 'album_name': '', 'duration_ms': duration_ms, 'duration': format_track_duration(duration_ms), 'disc_number': 1, 'track_number': index})
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        stack.append(child)
            elif isinstance(value, list):
                for child in value:
                    if isinstance(child, (dict, list)):
                        stack.append(child)
    return tracks

def select_tracks_for_album(tracks, artist, album_title):
    if not tracks:
        return []
    target_album = normalize_text(album_title)
    target_artist = normalize_text(artist)
    album_groups = {}
    for track in tracks:
        album_name = track.get('album_name', '')
        if not album_name:
            continue
        key = normalize_text(album_name)
        if not key:
            continue
        album_groups.setdefault(key, {'name': album_name, 'tracks': []})['tracks'].append(track)
    if album_groups:
        best_group = None
        best_score = -1
        for group in album_groups.values():
            group_name = group['name']
            title_score = similarity(album_title, group_name)
            score = int(title_score * 2000)
            if normalize_text(group_name) == target_album:
                score += 3000
            elif target_album and (target_album in normalize_text(group_name) or normalize_text(group_name) in target_album):
                score += 1000
            group_artist_scores = [similarity(artist, track.get('artist', '')) for track in group['tracks'] if track.get('artist')]
            if group_artist_scores:
                score += int(max(group_artist_scores) * 500)
            if score > best_score:
                best_score = score
                best_group = group['tracks']
        if best_group and best_score >= 700:
            tracks = best_group
    numbered = [track for track in tracks if track.get('track_number', 0) > 0]
    if len(numbered) >= 2:
        tracks = numbered
    filtered = []
    for track in tracks:
        track_artist = track.get('artist', '')
        if target_artist and track_artist and (similarity(artist, track_artist) < 0.25):
            continue
        filtered.append(track)
    if filtered:
        tracks = filtered
    tracks = sorted(tracks, key=lambda item: (item.get('disc_number', 1), item.get('track_number', 0) or 9999, item.get('title', '')))
    next_number_by_disc = {}
    for track in tracks:
        disc = track.get('disc_number', 1) or 1
        if track.get('track_number', 0) <= 0:
            current = next_number_by_disc.get(disc, 0) + 1
            track['track_number'] = current
            next_number_by_disc[disc] = current
        else:
            next_number_by_disc[disc] = max(next_number_by_disc.get(disc, 0), track['track_number'])
    return tracks

def extract_tracks_from_apple_music_page(page, artist, album_title):
    if not page:
        return []
    parser = page['parser']
    candidates = []
    for raw in parser.serialized_scripts:
        data = safe_json_decode(raw)
        if not data:
            continue
        candidates.extend(traverse_for_track_objects(data))
    selected = select_tracks_for_album(candidates, artist, album_title)
    if selected:
        print('APPLE WEB SERIALIZED TRACK COUNT:', len(selected))
        return selected
    ld_tracks = extract_jsonld_tracks(parser)
    selected = select_tracks_for_album(ld_tracks, artist, album_title)
    if selected:
        print('APPLE WEB JSON-LD TRACK COUNT:', len(selected))
    return selected

def track_from_itunes_item(item):
    track_name = item.get('trackName', '')
    if not track_name:
        return None
    try:
        disc_number = int(item.get('discNumber', 1) or 1)
    except Exception:
        disc_number = 1
    try:
        track_number = int(item.get('trackNumber', 0) or 0)
    except Exception:
        track_number = 0
    duration_ms = item.get('trackTimeMillis') or 0
    try:
        duration_ms = int(duration_ms)
    except Exception:
        duration_ms = 0
    return {'id': str(item.get('trackId') or ''), 'title': track_name, 'artist': item.get('artistName', ''), 'album_name': item.get('collectionName', ''), 'duration_ms': duration_ms, 'duration': format_track_duration(duration_ms), 'disc_number': disc_number, 'track_number': track_number}

def lookup_kr_tracks_by_id(collection_id):
    collection_id = str(collection_id or '').strip()
    if not collection_id:
        return []
    try:
        response = APPLE_SESSION.get('https://itunes.apple.com/lookup', params={'id': collection_id, 'entity': 'song', 'country': 'KR', 'limit': 300, 'explicit': 'Yes'}, timeout=12)
        response.raise_for_status()
        data = response.json()
        tracks = []
        for item in data.get('results', []):
            if item.get('wrapperType') != 'track':
                continue
            if item.get('kind') != 'song':
                continue
            track = track_from_itunes_item(item)
            if track:
                tracks.append(track)
        tracks.sort(key=lambda item: (item['disc_number'], item['track_number']))
        print('ITUNES KR LOOKUP TRACK COUNT:', len(tracks))
        return tracks
    except Exception as e:
        print('ITUNES KR TRACK ERROR:', e)
        return []

def search_kr_album_candidates(search_term, use_album_attribute=False):
    if not search_term:
        return []
    try:
        params = {'term': search_term, 'country': 'KR', 'media': 'music', 'entity': 'album', 'limit': 100, 'explicit': 'Yes'}
        if use_album_attribute:
            params['attribute'] = 'albumTerm'
        response = APPLE_SESSION.get('https://itunes.apple.com/search', params=params, timeout=12)
        response.raise_for_status()
        data = response.json()
        results = []
        for item in data.get('results', []):
            collection_id = item.get('collectionId')
            if not collection_id:
                continue
            title = item.get('collectionName', '')
            if not title:
                continue
            release_date = item.get('releaseDate', '')[:10]
            results.append({'collection_id': str(collection_id), 'artist': item.get('artistName', ''), 'title': title, 'release_year': release_date[:4] if release_date else '', 'collection_url': item.get('collectionViewUrl', ''), 'track_count': item.get('trackCount', 0) or 0})
        return results
    except Exception as e:
        print('KR ALBUM SEARCH ERROR:', e)
        return []

def find_kr_equivalent_album(artist, album_title, release_year=''):
    artist = str(artist or '').strip()
    album_title = str(album_title or '').strip()
    release_year = str(release_year or '').strip()
    if not album_title:
        return None
    searches = [(album_title, True), (f'{artist} {album_title}'.strip(), False)]
    candidates = []
    seen = set()
    for term, use_attribute in searches:
        if not term:
            continue
        found = search_kr_album_candidates(term, use_attribute)
        for candidate in found:
            collection_id = candidate['collection_id']
            if collection_id in seen:
                continue
            seen.add(collection_id)
            candidates.append(candidate)
    if not candidates:
        return None
    target_has_korean = contains_hangul(album_title)
    best = None
    best_score = -1
    for candidate in candidates:
        compare_title = candidate['title']
        if target_has_korean and (not contains_hangul(compare_title)):
            compare_title = get_kr_localized_album_title(candidate['collection_id'], candidate['collection_url'], compare_title)
        title_score = similarity(album_title, compare_title)
        artist_score = similarity(artist, candidate['artist']) if artist else 0
        score = int(title_score * 1800)
        score += int(artist_score * 700)
        if normalize_text(compare_title) == normalize_text(album_title):
            score += 2500
        if release_year and candidate['release_year'] == release_year:
            score += 500
        candidate['localized_title'] = compare_title
        print('KR ALBUM CANDIDATE:', candidate['collection_id'], compare_title, candidate['artist'], candidate['release_year'], 'SCORE=', score)
        if score > best_score:
            best_score = score
            best = candidate
    if not best or best_score < 550:
        return None
    print('KR ALBUM MATCH:', best['collection_id'], best.get('localized_title', best['title']))
    return best

def search_kr_tracks_by_term(artist, album_title, release_year=''):
    terms = []
    combined = f'{artist} {album_title}'.strip()
    if combined:
        terms.append(combined)
    if album_title:
        terms.append(album_title)
    all_items = []
    seen_track_ids = set()
    for term in terms:
        try:
            response = APPLE_SESSION.get('https://itunes.apple.com/search', params={'term': term, 'country': 'KR', 'media': 'music', 'entity': 'song', 'limit': 200, 'explicit': 'Yes'}, timeout=12)
            response.raise_for_status()
            data = response.json()
            for item in data.get('results', []):
                if item.get('kind') != 'song':
                    continue
                track_id = str(item.get('trackId') or '')
                if track_id and track_id in seen_track_ids:
                    continue
                if track_id:
                    seen_track_ids.add(track_id)
                all_items.append(item)
        except Exception as e:
            print('KR SONG SEARCH ERROR:', e)
    if not all_items:
        return []
    groups = {}
    for item in all_items:
        collection_id = str(item.get('collectionId') or '')
        if not collection_id:
            continue
        groups.setdefault(collection_id, []).append(item)
    best_items = None
    best_score = -1
    for collection_id, items in groups.items():
        first = items[0]
        collection_name = first.get('collectionName', '')
        artist_name = first.get('artistName', '')
        release_date = first.get('releaseDate', '')[:10]
        year = release_date[:4] if release_date else ''
        collection_url = first.get('collectionViewUrl', '')
        localized_name = collection_name
        if contains_hangul(album_title) and (not contains_hangul(localized_name)):
            localized_name = get_kr_localized_album_title(collection_id, collection_url, collection_name)
        score = int(similarity(album_title, localized_name) * 2000)
        score += int(similarity(artist, artist_name) * 800)
        if normalize_text(album_title) == normalize_text(localized_name):
            score += 2200
        if release_year and year == release_year:
            score += 400
        score += len(items) * 10
        if score > best_score:
            best_score = score
            best_items = items
    if not best_items:
        return []
    tracks = []
    for item in best_items:
        track = track_from_itunes_item(item)
        if track:
            tracks.append(track)
    tracks.sort(key=lambda item: (item['disc_number'], item['track_number']))
    return tracks

def get_album_tracks(collection_id, artist='', album_title='', release_year=''):
    collection_id = str(collection_id or '').strip()
    cache_key = 'KR|' + collection_id + '|' + normalize_text(artist) + '|' + normalize_text(album_title) + '|' + str(release_year or '')
    cached = TRACK_CACHE.get(cache_key)
    if cached and time.time() - cached['time'] < TRACK_CACHE_SECONDS:
        return cached['data']
    if collection_id:
        page = fetch_apple_music_kr_page(collection_id)
        localized_match_title = get_kr_localized_album_title(collection_id, '', album_title) or album_title
        web_tracks = extract_tracks_from_apple_music_page(page, artist, localized_match_title)
        if web_tracks:
            result = {'tracks': web_tracks, 'store': 'KR', 'resolved_by': 'apple_music_web_stored_id'}
            TRACK_CACHE[cache_key] = {'time': time.time(), 'data': result}
            return result
        api_tracks = lookup_kr_tracks_by_id(collection_id)
        if api_tracks:
            result = {'tracks': api_tracks, 'store': 'KR', 'resolved_by': 'itunes_stored_id'}
            TRACK_CACHE[cache_key] = {'time': time.time(), 'data': result}
            return result
    kr_album = find_kr_equivalent_album(artist, album_title, release_year)
    if kr_album:
        kr_id = kr_album['collection_id']
        kr_url = kr_album.get('collection_url', '')
        page = fetch_apple_music_kr_page(kr_id, kr_url)
        localized_match_title = get_kr_localized_album_title(kr_id, kr_url, album_title) or album_title
        web_tracks = extract_tracks_from_apple_music_page(page, artist, localized_match_title)
        if web_tracks:
            result = {'tracks': web_tracks, 'store': 'KR', 'resolved_by': 'apple_music_web_kr_match'}
            TRACK_CACHE[cache_key] = {'time': time.time(), 'data': result}
            return result
        api_tracks = lookup_kr_tracks_by_id(kr_id)
        if api_tracks:
            result = {'tracks': api_tracks, 'store': 'KR', 'resolved_by': 'itunes_kr_match'}
            TRACK_CACHE[cache_key] = {'time': time.time(), 'data': result}
            return result
    tracks = search_kr_tracks_by_term(artist, album_title, release_year)
    result = {'tracks': tracks, 'store': 'KR', 'resolved_by': 'kr_song_search' if tracks else ''}
    TRACK_CACHE[cache_key] = {'time': time.time(), 'data': result}
    return result

