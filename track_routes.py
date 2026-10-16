from flask import (
    redirect,
    render_template,
    request,
    url_for,
)

from album_database import get_db
from track_service import (
    clear_album_track_cache,
    get_album_tracks,
    get_manual_album_tracks,
    has_manual_track_override,
    normalize_manual_duration,
)


def register_track_routes(app):
    app.add_url_rule(
        '/album/<int:album_row_id>/tracks',
        endpoint='edit_album_tracks',
        view_func=edit_album_tracks
    )
    app.add_url_rule(
        '/album/<int:album_row_id>/tracks/save',
        endpoint='save_album_tracks',
        view_func=save_album_tracks,
        methods=['POST']
    )
    app.add_url_rule(
        '/album/<int:album_row_id>/tracks/reset',
        endpoint='reset_album_tracks',
        view_func=reset_album_tracks,
        methods=['POST']
    )


def _positive_int(value, default=1):
    try:
        return max(
            1,
            int(value)
        )
    except Exception:
        return max(
            1,
            int(default)
        )


def _list_value(values, index, default=''):
    if index >= len(values):
        return default
    return values[index]


def _editor_track(track):
    return {
        'disc_number': _positive_int(
            track.get(
                'disc_number',
                1
            ),
            1
        ),
        'track_number': _positive_int(
            track.get(
                'track_number',
                1
            ),
            1
        ),
        'title': str(
            track.get('title', '')
            or ''
        ),
        'artist': str(
            track.get('artist', '')
            or ''
        ),
        'duration': str(
            track.get('duration', '')
            or ''
        ),
    }


def edit_album_tracks(album_row_id):
    track_saved = request.args.get(
        'track_saved',
        ''
    ).strip()
    track_reset = request.args.get(
        'track_reset',
        ''
    ).strip()

    conn = get_db()

    try:
        album = conn.execute(
            'SELECT * FROM albums '
            'WHERE id = ?',
            (album_row_id,)
        ).fetchone()

        if album is None:
            return redirect(
                url_for('index')
            )

        manual_track_mode = (
            has_manual_track_override(
                conn,
                album['album_id']
            )
        )

        tracks = (
            get_manual_album_tracks(
                conn,
                album['album_id']
            )
            if manual_track_mode
            else []
        )
    finally:
        conn.close()

    if not manual_track_mode:
        track_data = get_album_tracks(
            collection_id=(
                album[
                    'apple_collection_id'
                ]
                or ''
            ),
            artist=(
                album['artist']
                or ''
            ),
            album_title=(
                album['album_title']
                or ''
            ),
            release_year=(
                album['release_year']
                or ''
            ),
            album_id=(
                album['album_id']
                or ''
            )
        )
        tracks = track_data.get(
            'tracks',
            []
        )

    editor_tracks = [
        _editor_track(track)
        for track in tracks
    ]

    if not editor_tracks:
        editor_tracks = [{
            'disc_number': 1,
            'track_number': 1,
            'title': '',
            'artist': '',
            'duration': '',
        }]

    return render_template(
        'track_edit.html',
        album=album,
        tracks=editor_tracks,
        manual_track_mode=(
            manual_track_mode
        ),
        track_saved=track_saved,
        track_reset=track_reset
    )


def save_album_tracks(album_row_id):
    conn = get_db()

    try:
        album = conn.execute(
            'SELECT * FROM albums '
            'WHERE id = ?',
            (album_row_id,)
        ).fetchone()

        if album is None:
            return redirect(
                url_for('index')
            )

        disc_values = (
            request.form.getlist(
                'disc_number'
            )
        )
        track_values = (
            request.form.getlist(
                'track_number'
            )
        )
        title_values = (
            request.form.getlist(
                'title'
            )
        )
        artist_values = (
            request.form.getlist(
                'track_artist'
            )
        )
        duration_values = (
            request.form.getlist(
                'duration'
            )
        )

        rows = []

        for index, raw_title in enumerate(
            title_values
        ):
            title = str(
                raw_title
                or ''
            ).strip()

            if not title:
                continue

            disc_number = _positive_int(
                _list_value(
                    disc_values,
                    index,
                    1
                ),
                1
            )
            track_number = _positive_int(
                _list_value(
                    track_values,
                    index,
                    index + 1
                ),
                index + 1
            )
            artist = str(
                _list_value(
                    artist_values,
                    index,
                    ''
                )
                or ''
            ).strip()

            raw_duration = _list_value(
                duration_values,
                index,
                ''
            )
            duration = (
                normalize_manual_duration(
                    raw_duration
                )
                if raw_duration
                else ''
            )

            rows.append({
                'disc_number':
                    disc_number,
                'track_number':
                    track_number,
                'title': title,
                'artist': artist,
                'duration': duration,
            })

        rows.sort(
            key=lambda item: (
                item['disc_number'],
                item['track_number'],
                item['title'].casefold(),
            )
        )

        conn.execute(
            'DELETE FROM album_tracks '
            'WHERE album_id = ?',
            (album['album_id'],)
        )

        for row in rows:
            conn.execute(
                '''
                INSERT INTO album_tracks (
                    album_id,
                    disc_number,
                    track_number,
                    title,
                    artist,
                    duration
                ) VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (
                    album['album_id'],
                    row['disc_number'],
                    row['track_number'],
                    row['title'],
                    row['artist'],
                    row['duration'],
                )
            )

        conn.execute(
            '''
            INSERT INTO album_track_settings (
                album_id,
                manual_override,
                updated_at
            ) VALUES (?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(album_id)
            DO UPDATE SET
                manual_override = 1,
                updated_at = CURRENT_TIMESTAMP
            ''',
            (album['album_id'],)
        )

        clear_album_track_cache(
            album['album_id'],
            conn=conn
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return redirect(
        url_for(
            'edit_album_tracks',
            album_row_id=album_row_id,
            track_saved='1'
        )
    )


def reset_album_tracks(album_row_id):
    conn = get_db()

    try:
        album = conn.execute(
            'SELECT * FROM albums '
            'WHERE id = ?',
            (album_row_id,)
        ).fetchone()

        if album is None:
            return redirect(
                url_for('index')
            )

        conn.execute(
            'DELETE FROM album_tracks '
            'WHERE album_id = ?',
            (album['album_id'],)
        )
        conn.execute(
            'DELETE FROM album_track_settings '
            'WHERE album_id = ?',
            (album['album_id'],)
        )
        clear_album_track_cache(
            album['album_id'],
            conn=conn
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return redirect(
        url_for(
            'edit_album_tracks',
            album_row_id=album_row_id,
            track_reset='1'
        )
    )
