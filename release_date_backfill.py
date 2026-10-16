from album_database import get_db
from search_engine import APPLE_SESSION


STORES = (
    'KR',
    'US',
    'JP',
    'GB',
)
BATCH_SIZE = 50


def _clean_release_date(value):
    value = str(
        value
        or ''
    ).strip()[:10]

    if (
        len(value) == 10
        and value[4] == '-'
        and value[7] == '-'
        and value.replace(
            '-',
            ''
        ).isdigit()
    ):
        return value

    return ''


def _chunks(values, size):
    for index in range(
        0,
        len(values),
        size
    ):
        yield values[
            index:
            index + size
        ]


def _lookup_batch(
    collection_ids,
    store
):
    if not collection_ids:
        return {}

    try:
        response = APPLE_SESSION.get(
            'https://itunes.apple.com/lookup',
            params={
                'id': ','.join(
                    collection_ids
                ),
                'country': store,
                'entity': 'album',
                'explicit': 'Yes',
            },
            timeout=15
        )
        response.raise_for_status()
    except Exception as exc:
        print(
            'RELEASE DATE LOOKUP ERROR',
            store,
            exc
        )
        return {}

    result = {}

    for item in response.json().get(
        'results',
        []
    ):
        collection_id = str(
            item.get(
                'collectionId'
            )
            or ''
        ).strip()
        release_date = (
            _clean_release_date(
                item.get(
                    'releaseDate'
                )
            )
        )

        if (
            collection_id
            and release_date
            and collection_id
            in collection_ids
        ):
            result[
                collection_id
            ] = release_date

    return result


def backfill_release_dates():
    conn = get_db()

    try:
        rows = conn.execute(
            '''
            SELECT DISTINCT apple_collection_id
            FROM albums
            WHERE apple_collection_id
                    IS NOT NULL
              AND TRIM(
                    apple_collection_id
                  ) != ''
              AND (
                    release_date IS NULL
                    OR TRIM(
                        release_date
                    ) = ''
                  )
            '''
        ).fetchall()

        collection_ids = sorted({
            str(
                row[
                    'apple_collection_id'
                ]
                or ''
            ).strip()
            for row in rows
            if str(
                row[
                    'apple_collection_id'
                ]
                or ''
            ).strip().isdigit()
        })

        if not collection_ids:
            print(
                'RELEASE DATE BACKFILL: '
                'nothing to update'
            )
            return {
                'requested': 0,
                'updated': 0,
                'missing': 0,
            }

        unresolved = set(
            collection_ids
        )
        found = {}

        for store in STORES:
            if not unresolved:
                break

            pending = sorted(
                unresolved
            )

            for batch in _chunks(
                pending,
                BATCH_SIZE
            ):
                batch_result = (
                    _lookup_batch(
                        batch,
                        store
                    )
                )

                for (
                    collection_id,
                    release_date,
                ) in batch_result.items():
                    found[
                        collection_id
                    ] = release_date
                    unresolved.discard(
                        collection_id
                    )

        for (
            collection_id,
            release_date,
        ) in found.items():
            release_year = (
                release_date[:4]
            )

            conn.execute(
                '''
                UPDATE albums
                SET
                    release_date = ?,
                    release_year = ?
                WHERE apple_collection_id = ?
                ''',
                (
                    release_date,
                    release_year,
                    collection_id,
                )
            )

            conn.execute(
                '''
                UPDATE album_master
                SET
                    release_date = ?,
                    release_year = ?
                WHERE apple_collection_id = ?
                ''',
                (
                    release_date,
                    release_year,
                    collection_id,
                )
            )

        conn.commit()

        print(
            'RELEASE DATE BACKFILL:',
            len(collection_ids),
            'requested /',
            len(found),
            'updated /',
            len(unresolved),
            'missing'
        )

        return {
            'requested':
                len(collection_ids),
            'updated':
                len(found),
            'missing':
                len(unresolved),
        }
    except Exception as exc:
        conn.rollback()
        print(
            'RELEASE DATE BACKFILL ERROR:',
            exc
        )
        return {
            'requested': 0,
            'updated': 0,
            'missing': 0,
        }
    finally:
        conn.close()


if __name__ == '__main__':
    backfill_release_dates()
