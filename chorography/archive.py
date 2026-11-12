"""The harvest archive: raw payloads kept by content hash.

The archive is the harvester's memory of what the public web actually
sent. Every payload a fetcher receives is stored here before anything
parses it, laid out by the date of the fetch and the source that sent
it, in a file named by the SHA-256 of its bytes. Because the name is
the hash, storing the same bytes twice stores them once, a stored
payload can be checked against its name at any later date, and a
parser bug fixed next month can be re-run over history exactly as it
arrived, without asking the network to repeat itself.

Alongside the payload tree sits an index, one JSON line per fetch,
recording the source, the URL, the fetch time, the HTTP status, the
byte count, the payload hash and the inbox files the fetch produced.
The index is append-only, like the event log in the store: entries are
never edited or removed, and a later correction is a later line.

Nothing in this module reads the payloads back into the world. The
archive serves audit and re-derivation; the inbox serves the world.
"""

import hashlib
import json
import os
import re

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def sha256_hex(raw):
    return hashlib.sha256(raw).hexdigest()


def _safe(segment):
    cleaned = _SAFE_NAME.sub("-", str(segment)).strip("-.")
    return cleaned or "source"


class Archive:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.index_path = os.path.join(root, "index.jsonl")

    def payload_path(self, source, fetched_at, digest):
        date_part = str(fetched_at)[:10]
        pieces = [piece for piece in date_part.split("-") if piece]
        if len(pieces) != 3:
            pieces = ["undated", "", ""]
        return os.path.join(self.root, pieces[0], pieces[1], pieces[2],
                            _safe(source), digest + ".bin")

    def store(self, source, fetched_at, raw):
        """Store raw payload bytes and return their SHA-256 hex digest.

        The write happens only when the addressed file does not already
        exist, so an archive write never modifies bytes already kept.
        """
        digest = sha256_hex(raw)
        path = self.payload_path(source, fetched_at, digest)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(raw)
            os.replace(tmp, path)
        return digest

    def append_index(self, record):
        """Append one fetch record to the index as a JSON line."""
        with open(self.index_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def read_index(self):
        records = []
        if os.path.exists(self.index_path):
            with open(self.index_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        return records
