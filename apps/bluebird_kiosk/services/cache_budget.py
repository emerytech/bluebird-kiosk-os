"""A bounded, deterministic disk working set shared by photo and video downloads.

Filesystem sizes are authoritative, so an interrupted SQLite index update cannot
hide bytes. Only fixed native-cache names in the two configured roots are owned.
Readers need no write lock: a miss falls back to the cloud. No user/source files,
subdirectories, external symlink targets, database rows or school content are purged.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import shutil


class CacheBudget:
    def __init__(self, media_dir, video_dir, max_bytes=4*1024**3, min_free_bytes=1024**3):
        self.media_dir, self.video_dir = Path(media_dir), Path(video_dir)
        self.max_bytes, self.min_free_bytes = int(max_bytes), int(min_free_bytes)
        if self.max_bytes <= 0 or self.min_free_bytes < 0:
            raise ValueError("Invalid cache budget")

    def rank(self, path):
        path = Path(path)
        if path.parent == self.media_dir:
            m = re.fullmatch(r"([0-9]+)(\.thumb)?\.bin", path.name)
            if m: return (0 if m[2] else 2, int(m[1]), path.name)
        if path.parent == self.video_dir:
            if path.name in ("webm.bin", "mp4.bin", "poster.bin"):
                return (1, 0, path.name)
            m = re.fullmatch(r"rot-([0-9]+)-(webm|mp4|poster)\.bin", path.name)
            if m: return (3, int(m[1]), path.name)
        return None

    def entries(self):
        result = []
        for root in {self.media_dir, self.video_dir}:
            if not root.is_dir() or root.is_symlink():
                continue
            for path in root.iterdir():
                if self.rank(path) is None or path.is_symlink() or not path.is_file():
                    continue
                result.append((path, path.stat().st_size))
        return result

    @contextmanager
    def writer(self):
        if self.media_dir.is_symlink():
            raise ValueError("Cache root cannot be a symlink")
        fd = os.open(self.media_dir / '.cache-write.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                # Exclusive lock means no other native writer can own these.
                for root in {self.media_dir, self.video_dir}:
                    if root.is_symlink() or not root.is_dir(): continue
                    for part in root.glob(".cache-*.part"):
                        match = re.fullmatch(r"\.cache-(.+)\.[a-zA-Z0-9_-]+\.part", part.name)
                        if match and self.rank(root / match[1]) is not None:
                            part.unlink(missing_ok=True)
                yield self
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def prepare(self, target, reservation):
        """Reserve peak extra bytes, keeping old bytes until atomic replacement.

        A stable priority order (thumbs, designated video, originals, rotation;
        ascending id within a tier) prevents a too-large library oscillating on
        every tick. Only lower-priority bytes may be evicted for an incoming item.
        """
        target = Path(target)
        rank = self.rank(target)
        if rank is None or reservation < 0 or reservation > self.max_bytes:
            return False
        entries = self.entries()
        used = sum(size for _, size in entries)
        victims = sorted(((p, size) for p, size in entries if p != target and self.rank(p) > rank),
                         key=lambda item: self.rank(item[0]), reverse=True)
        while (used + reservation > self.max_bytes or
               shutil.disk_usage(target.parent).free < self.min_free_bytes + reservation):
            if not victims:
                return False
            path, size = victims.pop(0)
            path.unlink(missing_ok=True)
            used -= size
        return True

    def enforce(self):
        """Bound a pre-existing cache after configuration changes/restart."""
        entries = sorted(self.entries(), key=lambda item: self.rank(item[0]), reverse=True)
        used = sum(size for _, size in entries)
        removed = 0
        for path, size in entries:
            if used <= self.max_bytes and all(shutil.disk_usage(root).free >= self.min_free_bytes
                    for root in {self.media_dir, self.video_dir} if root.exists()):
                break
            path.unlink(missing_ok=True)
            used -= size
            removed += 1
        return {"bytes": used, "evicted": removed, "max_bytes": self.max_bytes}

    def check_free(self, parent, next_bytes):
        if shutil.disk_usage(parent).free < self.min_free_bytes + next_bytes:
            raise ValueError("Cache free-space reserve reached")
