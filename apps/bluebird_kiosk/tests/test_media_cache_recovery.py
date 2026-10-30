"""Offline regression coverage: no real server, devices, events, or notifications."""
import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
from bluebird_kiosk.services.local_cache import KioskLocalCache
from bluebird_kiosk.services.sync_client import SyncClient
from bluebird_kiosk.services.renderer import resolve_media_file_path
from .test_sync_client import FakeResponse


@pytest.fixture
def client(tmp_path):
    session = MagicMock(); session.headers = {}
    return SyncClient('https://fictional.invalid', 'fictional',
        KioskLocalCache(tmp_path/'cache.db'), tmp_path/'media', session=session)


def seed(client, version='v1'):
    client.cache.apply_rows('lw_media', [{'id': 1, 'media_version': version, 'published': 1}])
    path = client.media_dir/'1.bin'; path.write_bytes(b'old')
    client.cache.record_media_blob(1, str(path), '"old-etag"', 't1')
    return path


def manifest(client, rows, snapshot=True):
    body = {'cursor_at': 't2', 'rows': {'lw_media': rows}}
    if snapshot: body['snapshot_tables'] = ['lw_media']
    client.session.get.return_value = FakeResponse(json_body=body)
    return client.pull_manifest()


def test_hard_delete_after_reconnect_removes_metadata_index_and_disk(client):
    path = seed(client)
    client.cache = KioskLocalCache(client.cache._db_path)
    client.cache.set_cursor('old-before-offline')
    assert manifest(client, [])['ok']
    assert client.cache.get_row('lw_media', 1) is None
    assert client.cache.get_media_blob(1) is None
    assert not path.exists()
    assert resolve_media_file_path(client.cache, 1) is None


def test_delta_omission_is_not_deletion(client):
    path = seed(client)
    assert manifest(client, [], snapshot=False)['ok']
    assert path.read_bytes() == b'old'


@pytest.mark.parametrize('published', [False, 0])
def test_unpublish_revokes_cached_bytes(client, published):
    path = seed(client)
    assert manifest(client, [{'id': 1, 'media_version': 'v1', 'published': published}])['ok']
    assert not path.exists()
    assert resolve_media_file_path(client.cache, 1) is None


def test_replacement_invalidates_blob_and_downloads_new_bytes(client):
    path = seed(client)
    assert manifest(client, [{'id': 1, 'media_version': 'v2', 'published': 1}])['ok']
    assert not path.exists()
    client.session.get.return_value = FakeResponse(body_bytes=b'new', headers={'Content-Length':'3'})
    assert client.fetch_missing_media()['fetched'] == 1
    assert path.read_bytes() == b'new'
    assert client.cache.get_media_blob(1)['source_version'] == 'v2'


def test_caption_change_does_not_redownload_same_revision(client):
    path = seed(client)
    assert manifest(client, [{'id': 1, 'media_version': 'v1', 'published': 1, 'caption':'edited'}])['ok']
    client.session.get.reset_mock()
    assert client.fetch_missing_media()['skipped'] == 1
    client.session.get.assert_not_called()


@pytest.mark.parametrize('broken', ['missing','empty','truncated'])
def test_invalid_local_bytes_ignore_old_etag_and_retry_304(client, broken):
    path = seed(client)
    if broken == 'missing':path.unlink()
    elif broken == 'empty':path.write_bytes(b'')
    else:path.write_bytes(b'x')
    client.session.get.side_effect = [FakeResponse(status_code=304),
        FakeResponse(body_bytes=b'fixed', headers={'Content-Length':'5'})]
    assert client.fetch_missing_media()['fetched'] == 1
    assert path.read_bytes() == b'fixed'
    assert all('If-None-Match' not in call.kwargs['headers'] for call in client.session.get.call_args_list)


@pytest.mark.parametrize('headers,body', [({'Content-Length':'10'}, b'short'),
    ({'Content-Length':'1'}, b'too long'), ({'Content-Length':'0'},b''),
    ({'Content-Length':'bad'}, b'x'), ({'X-Content-SHA256':'wrong'},b'abc'),
    ({'Content-Encoding':'gzip'}, b'abc')])
def test_incomplete_or_corrupt_response_never_replaces_previous_file(client, headers, body):
    path = seed(client)
    client.session.get.return_value = FakeResponse(body_bytes=body, headers=headers)
    assert not client._download_media(1, client.cache.get_media_blob(1))
    assert path.read_bytes() == b'old'
    assert not list(client.media_dir.glob('*.part'))


def test_interrupted_stream_is_atomic_and_retryable(client):
    path = seed(client)
    response = FakeResponse(headers={'Content-Length':'8'})
    def chunks(**kwargs):
        yield b'partial'
        raise requests.exceptions.ChunkedEncodingError('fictional interruption')
    response.iter_content = chunks
    client.session.get.return_value = response
    assert not client._download_media(1, None)
    assert path.read_bytes() == b'old'
    assert not list(client.media_dir.glob('*.part'))


def test_successful_download_validates_hash(client):
    seed(client)
    client.session.get.return_value = FakeResponse(body_bytes=b'new', headers={
        'Content-Length':'3', 'X-Content-SHA256':hashlib.sha256(b'new').hexdigest()})
    assert client._download_media(1, None)
    assert client.cache.valid_media_blob(1)


@pytest.mark.parametrize('rows', [None, {}, [{'id':1},{'id':1}], [{'id':'1'}], [{'id':True}]])
def test_malformed_snapshot_never_deletes_or_advances_cursor(client, rows):
    path = seed(client); client.cache.set_cursor('keep')
    assert not manifest(client, rows)['ok']
    assert path.read_bytes() == b'old'
    assert client.cache.get_cursor() == 'keep'


def test_reconciliation_recovers_orphan_file_after_interrupted_purge(client):
    path = client.media_dir/'77.bin'; path.write_bytes(b'orphan')
    assert manifest(client, [])['ok']
    assert not path.exists()


def test_purge_never_follows_cached_symlink_outside_media_root(client, tmp_path):
    outside = tmp_path/'keep'; outside.write_bytes(b'protected')
    link = client.media_dir/'1.bin'; link.symlink_to(outside)
    client._purge_media_file(1)
    assert outside.read_bytes() == b'protected' and not link.exists()


def test_reopen_purges_orphan_without_network(client):
    path=client.media_dir/'88.bin';path.write_bytes(b'orphan')
    reopened=SyncClient(client.backend,client.token,KioskLocalCache(client.cache._db_path),client.media_dir,session=client.session)
    assert not path.exists()
    client.session.get.assert_not_called()


def test_snapshot_failure_rolls_back_metadata_and_cursor(client, monkeypatch):
    seed(client);client.cache.set_cursor('old')
    # Force an invalid JSON value in the second table after the first upsert.
    # The cache-level transaction must roll back everything, not advance cursor.
    with pytest.raises(TypeError):
        client.cache.apply_manifest({'lw_media':[{'id':1,'caption':'new'}],
            'lw_albums':[{'id':2,'not_json':object()}]},['lw_media','lw_albums'],'new')
    assert client.cache.get_row('lw_media',1)['media_version']=='v1'
    assert client.cache.get_cursor()=='old'


def test_extension_preserves_content_version_for_both_public_hosts():
    import json,re
    path=Path(__file__).parents[2]/'bluebird_kiosk_cache_ext/rules.json'
    rule=json.loads(path.read_text())[0]
    for host in ['bluebirdalerts.com','bluebird-alerts.com']:
        version='a'*64
        match=re.match(rule['condition']['regexFilter'],f'https://{host}/alpha/legacy-wall/media/7/image?v={version}')
        assert match and match.group(3)=='?v='+version


def test_metadata_replacement_during_download_cannot_mislabel_old_bytes(client):
    path=seed(client)
    response=FakeResponse(body_bytes=b'old stream')
    def chunks(**kwargs):
        client.cache.apply_rows('lw_media',[{'id':1,'media_version':'v2','published':1}])
        yield b'old stream'
    response.iter_content=chunks;client.session.get.return_value=response
    assert not client._download_media(1,None)
    assert path.read_bytes()==b'old'
    assert client.cache.valid_media_blob(1) is None


def test_html_proxy_error_is_not_cached_as_photo(client):
    path=seed(client)
    client.session.get.return_value=FakeResponse(body_bytes=b'<html>error</html>',headers={'Content-Type':'text/html'})
    assert not client._download_media(1,None)
    assert path.read_bytes()==b'old'


def test_response_without_length_or_digest_cannot_claim_complete(client):
    path=seed(client)
    response=FakeResponse(body_bytes=b'unknown completeness')
    response.headers.clear()
    client.session.get.return_value=response
    assert not client._download_media(1,None)
    assert path.read_bytes()==b'old'


def test_only_stale_atomic_download_staging_is_cleaned(client):
    import os,time
    stale=client.media_dir/'.1.crashed.part';stale.write_bytes(b'partial')
    fresh=client.media_dir/'.2.active.part';fresh.write_bytes(b'partial')
    unrelated=client.media_dir/'.source.part';unrelated.write_bytes(b'keep')
    os.utime(stale,(time.time()-90000,time.time()-90000))
    client._purge_orphan_media()
    assert not stale.exists() and fresh.exists() and unrelated.exists()
