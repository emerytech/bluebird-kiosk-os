"""Synthetic native-cache byte/variant contracts; no kiosk, cloud or alert actions."""
import hashlib
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from bluebird_kiosk.services.cache_budget import CacheBudget
from bluebird_kiosk.services.local_cache import KioskLocalCache
from bluebird_kiosk.services.sync_client import SyncClient
from .test_sync_client import FakeResponse
from .test_renderer import client  # actual loopback app fixture


def make(tmp_path, limit=1024*1024, reserve=0):
    session=MagicMock();session.headers={}
    return SyncClient('https://synthetic.invalid','synthetic',KioskLocalCache(tmp_path/'cache.db'),
        tmp_path/'media',session=session,video_dir=tmp_path/'video',
        cache_max_bytes=limit,cache_min_free_bytes=reserve)


def row(client,id=1):
    client.cache.apply_rows('lw_media',[{'id':id,'published':1,'media_version':'v1','media_variants':['image','thumb']}])


def test_thumb_and_original_have_independent_versions_and_bytes(tmp_path):
    c=make(tmp_path);row(c)
    def get(url,**kwargs):
        thumb='variant=thumb' in url;data=b'thumb' if thumb else b'original'
        return FakeResponse(body_bytes=data,headers={'X-Media-Version':'v1','X-Media-Variant':'thumb' if thumb else 'image','X-Content-SHA256':hashlib.sha256(data).hexdigest()})
    c.session.get.side_effect=get
    assert c.fetch_missing_media()['fetched']==2
    assert Path(c.cache.valid_media_blob(1,'thumb')['file_path']).read_bytes()==b'thumb'
    assert Path(c.cache.valid_media_blob(1)['file_path']).read_bytes()==b'original'
    assert c.fetch_missing_media()['skipped']==2
    c.session.get.return_value=FakeResponse(status_code=404);c.session.get.side_effect=None
    assert not c._download_media(1,c.cache.get_media_blob(1,'thumb'),'thumb')
    assert c.cache.valid_media_blob(1)  # missing thumbnail cannot revoke a good original
    c._purge_media_file(1)
    assert not list(c.media_dir.glob('*.bin'))
    assert c.cache.get_media_blob(1) is None and c.cache.get_media_blob(1,'thumb') is None


def test_old_server_does_not_get_unnegotiated_thumbnail_downloads(tmp_path):
    c=make(tmp_path);c.cache.apply_rows('lw_media',[{'id':1,'published':1}])
    c.session.get.return_value=FakeResponse(body_bytes=b'old-server-original')
    assert c.fetch_missing_media()['fetched']==1
    assert '?' not in c.session.get.call_args.args[0]


def test_combined_budget_restart_eviction_and_staging_cleanup(tmp_path):
    media=tmp_path/'media';video=tmp_path/'video';media.mkdir();video.mkdir()
    files={media/'1.thumb.bin':b't'*8,media/'1.bin':b'i'*16,video/'webm.bin':b'v'*16,video/'rot-2-mp4.bin':b'r'*16}
    for p,data in files.items():p.write_bytes(data)
    (video/'.cache-webm.bin.abcd.part').write_bytes(b'crashed')
    outside=tmp_path/'source';outside.write_bytes(b'preserve')
    (media/'99.bin').symlink_to(outside);(media/'notes.txt').write_bytes(b'keep')
    KioskLocalCache(tmp_path/'cache.db').apply_rows('lw_media',[{'id':1,'published':1}])
    c=make(tmp_path,limit=24)
    assert sum(size for _,size in c.budget.entries())==24
    assert (media/'1.thumb.bin').exists() and (video/'webm.bin').exists()
    assert not (media/'1.bin').exists() and not (video/'rot-2-mp4.bin').exists()
    assert not list(video.glob('*.part')) and not list(video.glob('.*.part'))
    assert outside.read_bytes()==b'preserve' and (media/'notes.txt').read_bytes()==b'keep'
    assert make(tmp_path,limit=24).budget.enforce()['bytes']==24


def test_over_capacity_library_has_stable_working_set_without_body_churn(tmp_path):
    c=make(tmp_path,limit=30)
    for id in range(1,9):row(c,id)
    downloads=[]
    class Response(FakeResponse):
        def iter_content(self,**kwargs):downloads.append(True);yield self._bytes
    c.session.get.side_effect=lambda url,**kwargs:Response(body_bytes=b'1234567890',headers={'X-Media-Variant':'thumb' if '?' in url else 'image','X-Media-Version':'v1'})
    assert c.fetch_missing_media()['fetched']==3
    working={p.name for p,_ in c.budget.entries()}
    assert working=={'1.thumb.bin','2.thumb.bin','3.thumb.bin'}
    for _ in range(3):assert c.fetch_missing_media()['fetched']==0
    assert len(downloads)==3 and {p.name for p,_ in c.budget.entries()}==working


def test_reserve_blocks_writes_and_midstream_pressure_keeps_old_bytes(tmp_path,monkeypatch):
    c=make(tmp_path,reserve=10);row(c)
    p=c.media_dir/'1.bin';p.write_bytes(b'old');c.cache.record_media_blob(1,str(p),'old','t0')
    c.session.get.return_value=FakeResponse(body_bytes=b'new')
    monkeypatch.setattr('bluebird_kiosk.services.cache_budget.shutil.disk_usage',lambda _:NS(free=9))
    assert not c._download_media(1,c.cache.get_media_blob(1));assert p.read_bytes()==b'old'
    monkeypatch.setattr('bluebird_kiosk.services.cache_budget.shutil.disk_usage',lambda _:NS(free=100))
    def pressure(*_):raise ValueError('synthetic full disk')
    monkeypatch.setattr(c.budget,'check_free',pressure)
    assert not c._download_media(1,c.cache.get_media_blob(1));assert p.read_bytes()==b'old'
    assert not list(c.media_dir.glob('.*.part'))


def test_missing_video_never_accepts_304_as_repair_and_failed_write_is_atomic(tmp_path):
    c=make(tmp_path);c.video_dir.mkdir();target=c.video_dir/'webm.bin'
    c.session.get.side_effect=[FakeResponse(status_code=304),FakeResponse(body_bytes=b'fixed')]
    assert c._fetch_blob_to_file('https://synthetic.invalid/video','old','webm.bin','test',file_size=5)[0]=='fetched'
    assert all('If-None-Match' not in call.kwargs['headers'] for call in c.session.get.call_args_list)
    assert target.read_bytes()==b'fixed'
    c.session.get.side_effect=None;c.session.get.return_value=FakeResponse(body_bytes=b'short',headers={'Content-Length':'9'})
    assert c._fetch_blob_to_file('https://synthetic.invalid/video','old','webm.bin','test',file_size=5)[0]=='errored'
    assert target.read_bytes()==b'fixed' and not list(c.video_dir.glob('.*.part'))


def test_video_cannot_exceed_combined_cache_budget(tmp_path):
    c=make(tmp_path,limit=8);c.video_dir.mkdir();(c.media_dir/'1.thumb.bin').write_bytes(b'keep')
    c.session.get.return_value=FakeResponse(body_bytes=b'123456789')
    assert c._fetch_blob_to_file('https://synthetic.invalid/video',None,'webm.bin','test')[0]=='errored'
    assert (c.media_dir/'1.thumb.bin').read_bytes()==b'keep' and not (c.video_dir/'webm.bin').exists()


def test_local_thumbnail_route_never_substitutes_original(client,monkeypatch):
    http,app,tmp=client
    endpoint=next(r.endpoint for r in app.routes if getattr(r,'path','')=='/legacy-wall/media/{media_id}/{variant}')
    monkeypatch.setattr(endpoint.__globals__['config'],'read_config',lambda:{'BLUEBIRD_BACKEND':'https://synthetic.invalid','SCHOOL_SLUG':'alpha'})
    version=app.state.local_cache.get_media_blob(1)['source_version']
    miss=http.get('/legacy-wall/media/1/thumb?v='+version,follow_redirects=False)
    assert miss.status_code==302 and '/thumb' in miss.headers['location'] and 'bounded=1' in miss.headers['location']
    thumb=tmp/'media'/'1.thumb.bin';thumb.write_bytes(b'RIFF0000WEBPthumbnail')
    app.state.local_cache.record_media_blob(1,str(thumb),'thumb','now',variant='thumb')
    response=http.get('/legacy-wall/media/1/thumb?v='+version)
    assert response.content==thumb.read_bytes() and response.content!=b'FAKEJPEG'
    thumb.write_bytes(b'')
    assert http.get('/legacy-wall/media/1/thumb?v='+version,follow_redirects=False).status_code==302


def test_video_purge_never_trusts_an_indexed_external_path(tmp_path):
    c=make(tmp_path);outside=tmp_path/'source';outside.write_bytes(b'keep')
    c.cache.record_video_blob('webm',str(outside),'etag','t0')
    c.cache.record_rotation_video_blob(7,'webm',str(outside),'etag','t0')
    c._purge_background_video();c._purge_rotation_videos_except(set())
    assert outside.read_bytes()==b'keep'


def test_failed_thumbnails_cannot_starve_originals_or_unbound_tick_requests(tmp_path):
    c=make(tmp_path)
    for id in range(1,20):row(c,id)
    c.session.get.side_effect=lambda url,**kw: FakeResponse(status_code=404) if '?' in url else FakeResponse(body_bytes=b'original')
    stats=c.fetch_missing_media(limit=2)
    assert stats['fetched']==2 and c.session.get.call_count==4


def test_cache_lock_cannot_follow_a_symlink(tmp_path):
    import pytest
    media=tmp_path/'media';media.mkdir();outside=tmp_path/'source';outside.write_bytes(b'keep')
    (media/'.cache-write.lock').symlink_to(outside)
    with pytest.raises(OSError):
        with CacheBudget(media,tmp_path/'video').writer():pass
    assert outside.read_bytes()==b'keep'


def test_cache_schema_upgrade_preserves_legacy_indexes_and_pending_events(tmp_path):
    import sqlite3
    path=tmp_path/'cache.db'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE media_blobs(media_id INTEGER PRIMARY KEY,etag TEXT,file_path TEXT,fetched_at TEXT);CREATE TABLE video_blobs(kind TEXT PRIMARY KEY,etag TEXT,file_path TEXT,fetched_at TEXT);')
        db.execute('INSERT INTO media_blobs VALUES(1,?,?,?)',('old-etag','old.bin','before'))
        db.execute('INSERT INTO video_blobs VALUES(?,?,?,?)',('webm','video-etag','old.webm','before'))
    cache=KioskLocalCache(path)
    assert cache.get_media_blob(1)['etag']=='old-etag' and cache.get_media_blob(1,'thumb') is None
    assert cache.get_video_blob('webm')['etag']=='video-etag'
    cache.queue_event('synthetic',{'keep':True},'before')
    assert KioskLocalCache(path).list_pending_events()[0]['event_data']=={'keep':True}
