"""Private append-only JSONL action journal; bounded recent reads, no mail bodies."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat


class PublicError(ValueError):
    """A controlled refusal safe to return to the MCP caller."""


class ActionLog:
    def __init__(self, filename):
        self.filename = Path(filename)

    def _open(self, write=False):
        if write:
            self.filename.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = self.filename.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise PublicError('Action log directory must be private and owned by the service account')
        flags = os.O_NOFOLLOW | (os.O_APPEND | os.O_CREAT | os.O_RDWR if write else os.O_RDONLY)
        fd = os.open(self.filename, flags, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            os.close(fd)
            raise PublicError('Action log must be an owned regular mode-0600 file without hard links')
        return os.fdopen(fd, 'ab' if write else 'rb')

    def append(self, entry):
        record = {'time': datetime.now(timezone.utc).isoformat(), **entry}
        raw = (json.dumps(record, ensure_ascii=True, separators=(',', ':')) + '\n').encode()
        with self._open(write=True) as stream:
            size = os.fstat(stream.fileno()).st_size
            if size and os.pread(stream.fileno(), 1, size - 1) != b'\n':
                stream.write(b'\n')  # Preserve/separate a torn prior record after a crash.
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    def recent(self, limit=50):
        try:
            stream = self._open()
        except FileNotFoundError:
            return {'entries': [], 'truncated': False}
        with stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            start = max(0, size - 2 * 1024 * 1024)
            stream.seek(start)
            if start:
                stream.readline()  # Discard partial first line.
            lines = stream.read().splitlines()
        entries = []
        malformed = False
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except (ValueError, UnicodeError):
                malformed = True
        return {'entries': entries, 'truncated': bool(start or len(lines) > limit),
                'incompleteLog': malformed}
