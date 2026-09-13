"""Storage backends for the published feed (architecture doc 3.4: "Written
to exports/<export_id>/ then promoted to latest/ if the integrity gate
passes").

Two backends behind one small interface:
- `LocalStorage` -- a plain directory tree. Used in dev and by the test
  suite; also usable in production if the export job and the web server
  share a filesystem (e.g. a mounted volume).
- `S3Storage` -- any S3-compatible object store (Cloudflare R2, MinIO, AWS
  S3) via boto3. Endpoint, bucket, and credentials come from environment
  variables only -- never hardcoded (per pact-security-patterns).

Promotion strategy: write every file under `exports/<export_id>/...`, then
copy each into `latest/...`, writing `meta.json` LAST. The site only ever
trusts a feed after reading `meta.json` (it's the first thing fetched and
carries `schema_version`), so a reader can never observe a `latest/` with
mismatched deals.json/products/*.json and a stale-but-valid meta.json --
worst case it sees the *previous* generation's meta.json alongside
already-fully-written new detail files, which is a superset, never a
partial write. This is a deliberate, documented simplification of "atomic
promote" (a true atomic directory swap needs object-store versioning or a
POSIX rename of a whole tree, neither of which is portably available across
both Local-on-Windows and S3-compatible backends) -- see HANDOFF.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Protocol


class Storage(Protocol):
    def write_versioned(self, export_id: str, files: dict[str, bytes]) -> None: ...

    def promote(self, export_id: str, files: dict[str, bytes]) -> None: ...

    def read_latest(self, rel_path: str) -> bytes | None: ...


def _order_meta_last(files: dict[str, bytes]) -> list[tuple[str, bytes]]:
    items = list(files.items())
    items.sort(key=lambda kv: (kv[0] == "meta.json",))
    return items


class LocalStorage:
    """Writes to `<base_dir>/exports/<export_id>/...` and
    `<base_dir>/latest/...`. Each file is written to a `.tmp` sibling then
    `os.replace()`d into place, which is atomic per-file on both POSIX and
    Windows (the property this task actually needs: no reader ever observes
    a half-written individual file)."""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)

    def _write_atomic(self, dest: Path, data: bytes) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)

    @staticmethod
    def _safe_dirname(export_id: str) -> str:
        """export_id (e.g. "2026-09-12T00:00:00Z-abcd") contains colons,
        which are valid in an S3 key but not in a Windows path component.
        Local dev on Windows needs this sanitized; the export_id recorded
        inside meta.json itself is never touched."""
        return export_id.replace(":", "-")

    def write_versioned(self, export_id: str, files: dict[str, bytes]) -> None:
        version_dir = self.base_dir / "exports" / self._safe_dirname(export_id)
        for rel_path, data in files.items():
            self._write_atomic(version_dir / rel_path, data)

    def promote(self, export_id: str, files: dict[str, bytes]) -> None:
        latest_dir = self.base_dir / "latest"
        for rel_path, data in _order_meta_last(files):
            self._write_atomic(latest_dir / rel_path, data)

    def read_latest(self, rel_path: str) -> bytes | None:
        path = self.base_dir / "latest" / rel_path
        if not path.exists():
            return None
        return path.read_bytes()

    def prune_versioned(self, keep: int = 5) -> None:
        """Deletes all but the `keep` most recently modified export
        directories under exports/ -- housekeeping only, never touches
        latest/. Not called automatically; the CLI exposes it via
        `fpt export --prune`."""
        exports_dir = self.base_dir / "exports"
        if not exports_dir.exists():
            return
        dirs = sorted(
            (d for d in exports_dir.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for stale in dirs[keep:]:
            shutil.rmtree(stale, ignore_errors=True)


class S3Storage:
    """S3-compatible backend (Cloudflare R2, MinIO, AWS S3). All connection
    details come from the constructor args, which the CLI populates from
    environment variables (`EXPORT_S3_*`) -- never from a config file, so a
    misconfigured deploy fails loudly instead of writing to the wrong
    bucket."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        key_prefix: str = "",
    ):
        import boto3  # imported lazily so LocalStorage-only paths never need boto3 installed

        self.bucket = bucket
        self.key_prefix = key_prefix.strip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )

    def _key(self, *parts: str) -> str:
        joined = "/".join(p.strip("/") for p in parts if p)
        return f"{self.key_prefix}/{joined}" if self.key_prefix else joined

    def _content_type(self, rel_path: str) -> str:
        return "application/json" if rel_path.endswith(".json") else "application/octet-stream"

    def write_versioned(self, export_id: str, files: dict[str, bytes]) -> None:
        for rel_path, data in files.items():
            self._client.put_object(
                Bucket=self.bucket,
                Key=self._key("exports", export_id, rel_path),
                Body=data,
                ContentType=self._content_type(rel_path),
            )

    def promote(self, export_id: str, files: dict[str, bytes]) -> None:
        for rel_path, data in _order_meta_last(files):
            self._client.put_object(
                Bucket=self.bucket,
                Key=self._key("latest", rel_path),
                Body=data,
                ContentType=self._content_type(rel_path),
                CacheControl="no-cache" if rel_path == "meta.json" else "public, max-age=300",
            )

    def read_latest(self, rel_path: str) -> bytes | None:
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=self._key("latest", rel_path))
        except self._client.exceptions.NoSuchKey:
            return None
        except Exception as exc:  # noqa: BLE001 - botocore raises ClientError for 404s too
            if "NoSuchKey" in str(exc) or "404" in str(exc):
                return None
            raise
        return resp["Body"].read()


def build_storage_from_env(env: dict[str, str] | None = None) -> Storage:
    """Selects and constructs a backend from environment variables. Kept
    separate from the CLI's argparse wiring so `fpt.export.runner` and
    tests can call it directly."""
    env = env if env is not None else dict(os.environ)
    backend = env.get("EXPORT_STORAGE_BACKEND", "local").strip().lower()

    if backend == "local":
        base_dir = env.get("EXPORT_LOCAL_DIR", "./exports_output")
        return LocalStorage(base_dir)

    if backend == "s3":
        bucket = env.get("EXPORT_S3_BUCKET")
        if not bucket:
            raise ValueError("EXPORT_S3_BUCKET is required when EXPORT_STORAGE_BACKEND=s3")
        return S3Storage(
            bucket=bucket,
            endpoint_url=env.get("EXPORT_S3_ENDPOINT_URL") or None,
            region_name=env.get("EXPORT_S3_REGION") or None,
            aws_access_key_id=env.get("EXPORT_S3_ACCESS_KEY_ID") or env.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=env.get("EXPORT_S3_SECRET_ACCESS_KEY") or env.get("AWS_SECRET_ACCESS_KEY"),
            key_prefix=env.get("EXPORT_S3_KEY_PREFIX", ""),
        )

    raise ValueError(f"unknown EXPORT_STORAGE_BACKEND: {backend!r} (expected 'local' or 's3')")
