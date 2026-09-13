"""Tests for fpt.export.storage: LocalStorage (real filesystem, tmp_path)
and S3Storage (moto-mocked -- no real network or credentials). Both must
satisfy: versioned writes never touch latest/, promote() writes meta.json
last, and read_latest() round-trips."""

from __future__ import annotations

import json

import pytest

from fpt.export.storage import LocalStorage, build_storage_from_env


def _files():
    return {
        "meta.json": b'{"schema_version": 2}',
        "deals.json": b'{"deals": []}',
        "products/test-rod.json": b'{"slug": "test-rod"}',
    }


class TestLocalStorage:
    def test_write_versioned_does_not_touch_latest(self, tmp_path):
        storage = LocalStorage(tmp_path)
        storage.write_versioned("2026-09-12T00:00:00Z-abcd", _files())

        # export_id contains colons (S3-safe, not Windows-path-safe) --
        # LocalStorage sanitizes them for the on-disk directory name only.
        version_dir = tmp_path / "exports" / "2026-09-12T00-00-00Z-abcd"
        assert (version_dir / "meta.json").exists()
        assert (version_dir / "products" / "test-rod.json").exists()
        assert not (tmp_path / "latest").exists()

    def test_promote_writes_all_files_to_latest(self, tmp_path):
        storage = LocalStorage(tmp_path)
        files = _files()
        storage.promote("2026-09-12T00:00:00Z-abcd", files)

        for rel_path, data in files.items():
            written = (tmp_path / "latest" / rel_path).read_bytes()
            assert written == data

    def test_promote_overwrites_previous_latest(self, tmp_path):
        storage = LocalStorage(tmp_path)
        storage.promote("v1", {"meta.json": b'{"export_id": "v1"}'})
        storage.promote("v2", {"meta.json": b'{"export_id": "v2"}'})

        latest_meta = json.loads((tmp_path / "latest" / "meta.json").read_bytes())
        assert latest_meta["export_id"] == "v2"

    def test_promote_writes_meta_json_last(self, tmp_path, monkeypatch):
        storage = LocalStorage(tmp_path)
        write_order = []

        original = storage._write_atomic

        def spy(dest, data):
            write_order.append(dest.name)
            return original(dest, data)

        monkeypatch.setattr(storage, "_write_atomic", spy)
        storage.promote("v1", _files())

        assert write_order[-1] == "meta.json"

    def test_read_latest_returns_none_when_missing(self, tmp_path):
        storage = LocalStorage(tmp_path)
        assert storage.read_latest("meta.json") is None

    def test_read_latest_round_trips(self, tmp_path):
        storage = LocalStorage(tmp_path)
        storage.promote("v1", _files())
        assert storage.read_latest("meta.json") == _files()["meta.json"]

    def test_prune_versioned_keeps_only_n_most_recent(self, tmp_path):
        storage = LocalStorage(tmp_path)
        for i in range(7):
            storage.write_versioned(f"v{i}", {"meta.json": b"{}"})
        storage.prune_versioned(keep=3)

        remaining = list((tmp_path / "exports").iterdir())
        assert len(remaining) == 3

    def test_build_storage_from_env_defaults_to_local(self, tmp_path):
        env = {"EXPORT_STORAGE_BACKEND": "local", "EXPORT_LOCAL_DIR": str(tmp_path)}
        storage = build_storage_from_env(env)
        assert isinstance(storage, LocalStorage)
        assert storage.base_dir == tmp_path

    def test_build_storage_from_env_rejects_unknown_backend(self):
        with pytest.raises(ValueError, match="unknown EXPORT_STORAGE_BACKEND"):
            build_storage_from_env({"EXPORT_STORAGE_BACKEND": "carrier-pigeon"})

    def test_build_storage_from_env_s3_requires_bucket(self):
        with pytest.raises(ValueError, match="EXPORT_S3_BUCKET"):
            build_storage_from_env({"EXPORT_STORAGE_BACKEND": "s3"})


class TestS3StorageWithMoto:
    """No real network: moto intercepts boto3 calls at the botocore layer."""

    @pytest.fixture(autouse=True)
    def _moto_s3(self):
        moto = pytest.importorskip("moto")
        with moto.mock_aws():
            yield

    def _make_storage(self):
        import boto3

        from fpt.export.storage import S3Storage

        bucket = "fpt-test-bucket"
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
        return S3Storage(bucket=bucket, region_name="us-east-1")

    def test_write_versioned_and_promote_round_trip(self):
        storage = self._make_storage()
        files = _files()

        storage.write_versioned("2026-09-12T00:00:00Z-abcd", files)
        assert storage.read_latest("meta.json") is None  # versioned write must not touch latest/

        storage.promote("2026-09-12T00:00:00Z-abcd", files)
        assert storage.read_latest("meta.json") == files["meta.json"]
        assert storage.read_latest("products/test-rod.json") == files["products/test-rod.json"]

    def test_build_storage_from_env_s3(self):
        import boto3

        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="env-bucket")
        env = {
            "EXPORT_STORAGE_BACKEND": "s3",
            "EXPORT_S3_BUCKET": "env-bucket",
            "EXPORT_S3_REGION": "us-east-1",
        }
        storage = build_storage_from_env(env)
        storage.promote("v1", {"meta.json": b'{"ok": true}'})
        assert storage.read_latest("meta.json") == b'{"ok": true}'
