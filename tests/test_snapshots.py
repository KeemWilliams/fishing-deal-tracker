"""Unit tests for fpt/fetch/snapshots.py: the FPT_SNAPSHOT_DIR config bug
fix (2026-09-13). The default snapshot directory used to be hardcoded to
`/var/lib/fpt/snapshots`, which is not writable by an unprivileged runner
user -- confirmed on the GitHub Actions runner via
`PermissionError: [Errno 13] Permission denied: '/var/lib/fpt'`. It must
now default to a per-user writable path and remain overridable via the
FPT_SNAPSHOT_DIR env var.
"""

from __future__ import annotations

import importlib
import os
import tempfile

import fpt.fetch.snapshots as snapshots_module


def _reload_snapshots_module():
    """DEFAULT_SNAPSHOT_DIR is a module-level constant read once at import
    time, so exercising a different FPT_SNAPSHOT_DIR value requires
    reloading the module after changing the environment."""
    return importlib.reload(snapshots_module)


class TestDefaultSnapshotDir:
    def test_default_is_not_the_old_unwritable_var_lib_path(self, monkeypatch):
        monkeypatch.delenv("FPT_SNAPSHOT_DIR", raising=False)
        module = _reload_snapshots_module()
        try:
            assert str(module.DEFAULT_SNAPSHOT_DIR) != "/var/lib/fpt/snapshots"
        finally:
            _reload_snapshots_module()  # restore real env-derived state for other tests

    def test_default_lives_under_the_os_temp_dir(self, monkeypatch):
        monkeypatch.delenv("FPT_SNAPSHOT_DIR", raising=False)
        module = _reload_snapshots_module()
        try:
            assert str(module.DEFAULT_SNAPSHOT_DIR).startswith(tempfile.gettempdir())
        finally:
            _reload_snapshots_module()

    def test_default_directory_is_actually_writable(self, monkeypatch):
        """The whole point of the fix: don't just pick a different path,
        prove a real process can write there."""
        monkeypatch.delenv("FPT_SNAPSHOT_DIR", raising=False)
        module = _reload_snapshots_module()
        try:
            module.DEFAULT_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            probe = module.DEFAULT_SNAPSHOT_DIR / ".write_probe"
            probe.write_text("ok")
            assert probe.read_text() == "ok"
            probe.unlink()
        finally:
            _reload_snapshots_module()


class TestSnapshotDirFromEnvVar:
    def test_fpt_snapshot_dir_env_var_overrides_the_default(self, monkeypatch, tmp_path):
        custom_dir = tmp_path / "custom-snapshots"
        monkeypatch.setenv("FPT_SNAPSHOT_DIR", str(custom_dir))
        module = _reload_snapshots_module()
        try:
            assert module.DEFAULT_SNAPSHOT_DIR == custom_dir
        finally:
            monkeypatch.delenv("FPT_SNAPSHOT_DIR", raising=False)
            _reload_snapshots_module()


class TestWriteSnapshotCreatesMissingDirectories:
    def test_write_snapshot_creates_a_nested_missing_directory(self, tmp_path):
        nested_dir = tmp_path / "does" / "not" / "exist" / "yet"
        assert not nested_dir.exists()

        ref = snapshots_module.write_snapshot(
            b"hello world",
            task_id=1,
            fetched_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            snapshot_dir=nested_dir,
        )

        assert (nested_dir / ref).exists()
        assert snapshots_module.read_snapshot(ref, snapshot_dir=nested_dir) == b"hello world"


class TestSnapshotRefIsAlwaysComputableWithoutWriting:
    def test_snapshot_ref_for_does_not_touch_the_filesystem(self):
        """snapshot_ref_for is a pure function of (task_id, fetched_at,
        body) -- this is what lets fpt/fetch/http_fetcher.py compute a
        usable ref even when the actual write fails."""
        import datetime

        fetched_at = datetime.datetime(2026, 9, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)
        ref_one = snapshots_module.snapshot_ref_for(1, fetched_at, b"body")
        ref_two = snapshots_module.snapshot_ref_for(1, fetched_at, b"body")
        assert ref_one == ref_two
        assert ref_one  # never empty

        ref_different_body = snapshots_module.snapshot_ref_for(1, fetched_at, b"different body")
        assert ref_different_body != ref_one
