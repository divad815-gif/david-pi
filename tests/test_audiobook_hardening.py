import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
STREAMING_SOURCE = ROOT / "modules" / "audiobook_streaming.py"
STATE_PREPARER_SOURCE = ROOT / "deploy" / "david-pi-prepare-maintenance-state"


def load_isolated_streaming(data_root):
    name = f"audiobook_streaming_test_{uuid.uuid4().hex}"
    derivative_state = Path(data_root) / "operations" / "audiobook"
    environment = {
        "PHOTO_DATA": str(data_root),
        "DAVID_PI_AUDIOBOOKS_DATA": str(Path(data_root) / "audiobooks"),
        "DAVID_PI_AUDIOBOOK_DERIVATIVE_STATE": str(derivative_state),
        "DAVID_PI_AUDIOBOOK_PREPARE_MIN_FREE": "0",
    }
    with patch.dict(os.environ, environment):
        spec = importlib.util.spec_from_file_location(name, STREAMING_SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def load_state_preparer():
    name = f"audiobook_state_preparer_test_{uuid.uuid4().hex}"
    loader = SourceFileLoader(name, str(STATE_PREPARER_SOURCE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AudiobookHardeningTest(unittest.TestCase):
    def test_import_recovery_lease_elects_one_process_and_expires_boundedly(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            gate = threading.Barrier(3)
            claims = []
            errors = []
            def claim():
                try:
                    gate.wait(timeout=5)
                    claims.append(module.claim_import_recovery(60,current_epoch=1_000))
                except Exception as error:
                    errors.append(error)
            workers=[threading.Thread(target=claim) for _ in range(2)]
            for worker in workers: worker.start()
            gate.wait(timeout=5)
            for worker in workers: worker.join(5)
            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(errors,[])
            self.assertEqual(sorted(claims),[False,True])
            self.assertFalse(module.claim_import_recovery(60,current_epoch=1_059))
            self.assertTrue(module.claim_import_recovery(60,current_epoch=1_060))

    def test_old_queue_schema_migrates_under_concurrent_process_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            audiobook_root = data / "audiobooks"
            audiobook_root.mkdir()
            derivative_state = data / "operations" / "audiobook"
            derivative_state.mkdir(parents=True)
            queue = derivative_state / "playback-queue.db"
            with sqlite3.connect(queue) as connection:
                connection.execute(
                    """CREATE TABLE audiobook_playback_jobs(
                    book_id TEXT PRIMARY KEY,stored_name TEXT NOT NULL,
                    source_size INTEGER NOT NULL DEFAULT 0,source_sha256 TEXT,
                    state TEXT NOT NULL DEFAULT 'pending',format_version INTEGER NOT NULL DEFAULT 1,
                    derivative_bytes INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,updated_at TEXT NOT NULL,generated_at TEXT,error_code TEXT)"""
                )
            start = data / "start"
            code = (
                "import os,time; gate=os.environ['AUDIOBOOK_START_GATE'];"
                "\nwhile not os.path.exists(gate): time.sleep(.005)"
                "\nimport modules.audiobook_streaming"
            )
            environment = {
                **os.environ,
                "PYTHONPATH": str(ROOT),
                "PHOTO_DATA": str(data),
                "DAVID_PI_AUDIOBOOKS_DATA": str(audiobook_root),
                "DAVID_PI_AUDIOBOOK_DERIVATIVE_STATE": str(derivative_state),
                "DAVID_PI_AUDIOBOOK_PREPARE_MIN_FREE": "0",
                "AUDIOBOOK_START_GATE": str(start),
            }
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", code],
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(12)
            ]
            start.touch()
            failures = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                if process.returncode:
                    failures.append((process.returncode, stdout, stderr))
            self.assertEqual(failures, [])
            with sqlite3.connect(queue) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(audiobook_playback_jobs)")}
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"generation", "lease_token", "lease_expires_at"}.issubset(columns))
            self.assertTrue({
                "audiobook_catalog_snapshot",
                "audiobook_queue_metadata",
                "audiobook_import_reservations",
            }.issubset(tables))

    def test_queue_state_is_isolated_and_legacy_queue_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            legacy = data / "audiobooks" / "playback-queue.db"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"preserved legacy queue")
            module = load_isolated_streaming(data)
            self.assertEqual(
                module.QUEUE_DB,
                data / "operations" / "audiobook" / "playback-queue.db",
            )
            self.assertTrue(module.QUEUE_DB.is_file())
            self.assertEqual(legacy.read_bytes(), b"preserved legacy queue")

    def test_worker_catalog_is_authoritative_without_platform_mount(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            book_id = "a" * 32
            module.reconcile_catalog([{
                "id": book_id,
                "stored_name": f"{book_id}.mp3",
                "byte_size": 1024,
                "sha256": "source",
                "duration_seconds": 60,
                "owner_id": "must-not-enter-worker-snapshot",
                "title": "must-not-enter-worker-snapshot",
            }])
            forecast = module.reconcile_catalog_from_database()
            self.assertEqual(forecast["active_books"], 1)
            with module.queue_connection() as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(audiobook_catalog_snapshot)")}
                row = dict(connection.execute("SELECT * FROM audiobook_catalog_snapshot").fetchone())
            self.assertNotIn("owner_id", columns)
            self.assertNotIn("title", columns)
            self.assertEqual(row["book_id"], book_id)

    def test_periodic_cleanup_runs_after_catalog_reconciliation(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            book_id = "b" * 32
            module.reconcile_catalog([{
                "id": book_id,
                "stored_name": f"{book_id}.mp3",
                "byte_size": 1024,
                "sha256": "source",
                "duration_seconds": 60,
            }])
            parent = module.STREAMING / book_id
            old_version = parent / "v2"
            current_version = parent / f"v{module.FORMAT_VERSION}"
            old_version.mkdir(parents=True)
            current_version.mkdir()
            (old_version / "old").write_bytes(b"old")
            (current_version / "current").write_bytes(b"current")
            old_time = time.time() - (module.RETENTION_DAYS + 2) * 86400
            os.utime(old_version, (old_time, old_time))
            os.utime(current_version, (old_time, old_time))
            module._last_cleanup_monotonic = None
            with patch.object(module, "system_pause_reason", return_value="load_high"):
                result = module.work_once()
            self.assertEqual(result, {"state": "paused", "reason": "load_high"})
            self.assertFalse(old_version.exists())
            self.assertTrue(current_version.exists())

    def test_successful_job_triggers_post_publish_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            job = {"book_id": "c" * 32}
            forecast = {"fits": True}
            with (
                patch.object(module, "reconcile_catalog_from_database", return_value=forecast),
                patch.object(module, "maybe_cleanup_rebuildable_derivatives", side_effect=[0, 0]) as cleanup,
                patch.object(module, "system_pause_reason", return_value=None),
                patch.object(module, "claim_next_job", return_value=job),
                patch.object(module, "prepare_job", return_value=123),
            ):
                self.assertEqual(module.work_once(), {"state": "ready", "book_id": job["book_id"], "bytes": 123})
            self.assertEqual(cleanup.call_args_list, [call(), call(force=True)])

    def test_storage_forecast_reserves_atomic_staging_high_water(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            book_id = "d" * 32
            duration = 60
            target = module._derivative_byte_ceiling(
                module._forecast_duration_ceiling(duration), 2
            )
            current = module.STREAMING / book_id / f"v{module.FORMAT_VERSION}"
            current.mkdir(parents=True)
            with (current / "index.m4s").open("wb") as handle:
                handle.truncate(target)
            rows = [{"id": book_id, "byte_size": 1000, "duration_seconds": duration}]
            with patch.object(module.shutil, "disk_usage", return_value=SimpleNamespace(free=target - 1)):
                forecast = module.storage_forecast(rows)
            self.assertEqual(forecast["additional_bytes"], 0)
            self.assertEqual(forecast["staging_high_water_bytes"], target)
            self.assertEqual(forecast["required_free_bytes"], target)
            self.assertFalse(forecast["fits"])

    def test_storage_forecast_covers_validator_ceiling_overhead_and_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            book_id = "e" * 32
            duration = 3600.0
            accepted_duration = duration + max(2.0, duration * 0.02)
            derivative_ceiling = module._derivative_byte_ceiling(accepted_duration, 2)
            nominal_payload = int(duration * 96_000 / 8)
            with patch.object(module.shutil, "disk_usage", return_value=SimpleNamespace(free=module.MAX_FORECAST_BYTES)):
                forecast = module.storage_forecast([{
                    "id": book_id,
                    "stored_name": f"{book_id}.m4b",
                    "byte_size": 1,
                    "duration_seconds": duration,
                }])
            self.assertGreater(derivative_ceiling, int(accepted_duration * module.MAX_STEREO_BITRATE / 8))
            self.assertGreater(forecast["estimated_derivative_bytes"], nominal_payload)
            self.assertEqual(forecast["estimated_derivative_bytes"], derivative_ceiling)
            self.assertEqual(forecast["staging_high_water_bytes"], derivative_ceiling)
            self.assertEqual(forecast["required_free_bytes"], derivative_ceiling * 2)

    def test_forecast_rejects_nonfinite_or_impossible_arithmetic(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            book_id = "f" * 32
            base = {"id": book_id, "stored_name": f"{book_id}.m4b", "byte_size": 1}
            for duration in (float("nan"), float("inf"), float("-inf"), "Infinity"):
                with self.subTest(duration=duration), self.assertRaisesRegex(ValueError, "duration_invalid"):
                    module.storage_forecast([{**base, "duration_seconds": duration}])
            with self.assertRaisesRegex(ValueError, "forecast_overflow"):
                module.storage_forecast([{**base, "duration_seconds": 1e308}])
            for duration in (None, 0, -1):
                with self.subTest(duration=duration), self.assertRaisesRegex(ValueError, "duration_unavailable"):
                    module.storage_forecast([{**base, "duration_seconds": duration}])
            with patch.object(module.shutil, "disk_usage", return_value=SimpleNamespace(free=float("inf"))):
                with self.assertRaisesRegex(ValueError, "forecast_overflow"):
                    module.storage_forecast([{**base, "duration_seconds": 60}])

    def test_nonfinite_enqueue_and_reconcile_never_commit_queue_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            stable_id = "1" * 32
            stable = {
                "id": stable_id,
                "stored_name": f"{stable_id}.mp3",
                "byte_size": 1024,
                "sha256": "stable",
                "duration_seconds": 60,
            }
            module.reconcile_catalog([stable])
            with module.queue_connection() as connection:
                before_jobs = [tuple(row) for row in connection.execute(
                    "SELECT * FROM audiobook_playback_jobs ORDER BY book_id"
                )]
                before_catalog = [tuple(row) for row in connection.execute(
                    "SELECT * FROM audiobook_catalog_snapshot ORDER BY book_id"
                )]
                before_metadata = [tuple(row) for row in connection.execute(
                    "SELECT * FROM audiobook_queue_metadata ORDER BY key"
                )]

            rejected_id = "2" * 32
            for duration in (float("nan"), float("inf"), float("-inf"), "NaN"):
                with self.subTest(boundary="enqueue", duration=duration), self.assertRaisesRegex(ValueError, "duration_invalid"):
                    module.enqueue(rejected_id, f"{rejected_id}.mp3", 2048, "rejected", duration)
                with self.subTest(boundary="reconcile", duration=duration), self.assertRaisesRegex(ValueError, "duration_invalid"):
                    module.reconcile_catalog([
                        {**stable, "byte_size": 4096},
                        {
                            "id": rejected_id,
                            "stored_name": f"{rejected_id}.mp3",
                            "byte_size": 2048,
                            "sha256": "rejected",
                            "duration_seconds": duration,
                        },
                    ])
            for duration, code in ((None, "duration_unavailable"), (0, "duration_unavailable"), (-1, "duration_invalid")):
                with self.subTest(boundary="enqueue", duration=duration), self.assertRaisesRegex(ValueError, code):
                    module.enqueue(rejected_id, f"{rejected_id}.mp3", 2048, "rejected", duration)
            with self.assertRaisesRegex(ValueError, "duration_invalid"):
                module.reconcile_catalog([{
                    "id": "not-a-valid-id",
                    "stored_name": "ignored.mp3",
                    "byte_size": 1,
                    "duration_seconds": float("nan"),
                }])

            with module.queue_connection() as connection:
                after_jobs = [tuple(row) for row in connection.execute(
                    "SELECT * FROM audiobook_playback_jobs ORDER BY book_id"
                )]
                after_catalog = [tuple(row) for row in connection.execute(
                    "SELECT * FROM audiobook_catalog_snapshot ORDER BY book_id"
                )]
                after_metadata = [tuple(row) for row in connection.execute(
                    "SELECT * FROM audiobook_queue_metadata ORDER BY key"
                )]
            self.assertEqual(after_jobs, before_jobs)
            self.assertEqual(after_catalog, before_catalog)
            self.assertEqual(after_metadata, before_metadata)

    def test_enqueue_forecast_failure_is_atomic_and_never_creates_a_claimable_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            stable_id = "3" * 32
            new_id = "4" * 32
            module.reconcile_catalog([{
                "id": stable_id,
                "stored_name": f"{stable_id}.mp3",
                "byte_size": 100,
                "sha256": "stable",
                "duration_seconds": 60,
            }])
            observed = []

            def reject_forecast(rows):
                observed.extend(dict(row) for row in rows)
                raise ValueError("forecast_overflow")

            with patch.object(module, "storage_forecast", side_effect=reject_forecast):
                with self.assertRaisesRegex(ValueError, "forecast_overflow"):
                    module.enqueue(
                        new_id,
                        f"{new_id}.mp3",
                        200,
                        "new",
                        120,
                    )
            self.assertEqual({row["id"] for row in observed}, {stable_id, new_id})
            self.assertEqual(next(row for row in observed if row["id"] == new_id)["duration_seconds"], 120)
            with module.queue_connection() as connection:
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM audiobook_playback_jobs WHERE book_id=?", (new_id,)
                ).fetchone())
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM audiobook_catalog_snapshot WHERE book_id=?", (new_id,)
                ).fetchone())

    def test_concurrent_import_reservations_serialize_capacity_without_queue_visibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            first_id = "a" * 32
            second_id = "b" * 32
            row = {
                "id": first_id,
                "stored_name": f"{first_id}.mp3",
                "byte_size": 2048,
                "sha256": "1" * 64,
                "duration_seconds": 60,
            }
            with patch.object(
                module.shutil,"disk_usage",return_value=SimpleNamespace(free=module.MAX_FORECAST_BYTES)
            ):
                one_book_required = module.storage_forecast([row])["required_free_bytes"]
            with patch.object(
                module.shutil,"disk_usage",return_value=SimpleNamespace(free=one_book_required)
            ):
                module.reserve_import(
                    "c"*32,first_id,row["stored_name"],row["byte_size"],row["sha256"],
                    row["duration_seconds"],"first.upload.part",1,101,
                )
                with self.assertRaisesRegex(ValueError,"forecast_low_storage"):
                    module.reserve_import(
                        "d"*32,second_id,f"{second_id}.mp3",2048,"2"*64,60,
                        "second.upload.part",1,102,
                    )
            with module.queue_connection() as connection:
                reservations=[dict(item) for item in connection.execute(
                    "SELECT * FROM audiobook_import_reservations"
                )]
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_playback_jobs").fetchone()[0],0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_catalog_snapshot").fetchone()[0],0)
            self.assertEqual([item["book_id"] for item in reservations],[first_id])

    def test_import_abort_removes_only_exact_reserved_rows_and_preserves_existing_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            stable_id = "3" * 32
            imported_id = "4" * 32
            stable = {
                "id":stable_id,"stored_name":f"{stable_id}.mp3","byte_size":100,
                "sha256":"stable","duration_seconds":60,
            }
            with patch.object(
                module.shutil,"disk_usage",return_value=SimpleNamespace(free=module.MAX_FORECAST_BYTES)
            ):
                module.reconcile_catalog([stable])
                module.reserve_import(
                    "5"*32,imported_id,f"{imported_id}.mp3",200,"6"*64,120,
                    "import.upload.part",1,202,
                )
                module.mark_import_published("5"*32,imported_id)
                module.commit_reserved_import("5"*32,imported_id)
            aborting=module.begin_abort_import("5"*32,imported_id)
            self.assertEqual(aborting["state"],"aborting")
            module.finish_abort_import("5"*32,imported_id)
            with module.queue_connection() as connection:
                jobs=[dict(item) for item in connection.execute(
                    "SELECT * FROM audiobook_playback_jobs ORDER BY book_id"
                )]
                catalog=[dict(item) for item in connection.execute(
                    "SELECT * FROM audiobook_catalog_snapshot ORDER BY book_id"
                )]
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_import_reservations").fetchone()[0],0)
            self.assertEqual([item["book_id"] for item in jobs],[stable_id])
            self.assertEqual([item["book_id"] for item in catalog],[stable_id])

    def test_claim_reforecasts_current_catalog_before_changing_queue_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            book_id = "9" * 32
            module.reconcile_catalog([{
                "id": book_id,
                "stored_name": f"{book_id}.mp3",
                "byte_size": 100,
                "sha256": "source",
                "duration_seconds": 60,
            }])
            with patch.object(
                module.shutil, "disk_usage", return_value=SimpleNamespace(free=0)
            ), self.assertRaisesRegex(ValueError, "forecast_low_storage"):
                module.claim_next_job()
            with module.queue_connection() as connection:
                row = connection.execute(
                    "SELECT state,attempts,lease_token FROM audiobook_playback_jobs WHERE book_id=?",
                    (book_id,),
                ).fetchone()
            self.assertEqual((row["state"], row["attempts"], row["lease_token"]), ("pending", 0, None))

    def test_legacy_nonpositive_durations_remain_range_only_while_valid_jobs_continue(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            valid_id = "5" * 32
            zero_id = "6" * 32
            negative_id = "7" * 32
            rows = [
                {
                    "id": valid_id,
                    "stored_name": f"{valid_id}.mp3",
                    "byte_size": 100,
                    "sha256": "valid",
                    "duration_seconds": 60,
                },
                {
                    "id": zero_id,
                    "stored_name": f"{zero_id}.mp3",
                    "byte_size": 100,
                    "sha256": "zero",
                    "duration_seconds": 0,
                },
                {
                    "id": negative_id,
                    "stored_name": f"{negative_id}.mp3",
                    "byte_size": 100,
                    "sha256": "negative",
                    "duration_seconds": -1,
                },
            ]
            forecast = module.reconcile_catalog(rows)
            self.assertEqual(forecast["active_books"], 1)
            self.assertEqual(forecast["range_only_books"], 2)
            with module.queue_connection() as connection:
                jobs = {
                    row["book_id"]: dict(row)
                    for row in connection.execute("SELECT * FROM audiobook_playback_jobs")
                }
                catalog = {
                    row["book_id"]: dict(row)
                    for row in connection.execute("SELECT * FROM audiobook_catalog_snapshot")
                }
            self.assertEqual(jobs[valid_id]["state"], "pending")
            for book_id in (zero_id, negative_id):
                self.assertEqual(
                    (jobs[book_id]["state"], jobs[book_id]["error_code"]),
                    ("failed", "duration_unavailable"),
                )
                self.assertEqual(catalog[book_id]["active"], 0)
                self.assertEqual(catalog[book_id]["duration_seconds"], 0)
                self.assertEqual(module.playback_status(book_id)["mode"], "range")
                self.assertIsNone(module.derivative_paths(book_id))
            job = module.claim_next_job()
            self.assertEqual(job["book_id"], valid_id)
            self.assertEqual(job["forecast_duration_seconds"], 60)
            self.assertIsNone(module.claim_next_job())
            self.assertEqual(module.reconcile_catalog_from_database()["active_books"], 1)

    def test_prepare_rejects_source_duration_drift_before_transcode(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            book_id = "8" * 32
            source = module.ORIGINALS / f"{book_id}.m4b"
            source.write_bytes(b"local source")
            module.reconcile_catalog([{
                "id": book_id,
                "stored_name": source.name,
                "byte_size": source.stat().st_size,
                "sha256": module._source_sha256(source),
                "duration_seconds": 60,
            }])
            job = module.claim_next_job()
            with patch.object(module, "_probe", return_value={
                "codec": "aac", "channels": 2, "duration": 3600, "bit_rate": 96_000,
            }), patch.object(module.subprocess, "run") as transcode:
                with self.assertRaisesRegex(ValueError, "source_duration_mismatch"):
                    module.prepare_job(job)
            transcode.assert_not_called()
            with module.queue_connection() as connection:
                state = connection.execute(
                    "SELECT state,error_code FROM audiobook_playback_jobs WHERE book_id=?",
                    (book_id,),
                ).fetchone()
            self.assertEqual((state["state"], state["error_code"]), ("pending", "source_duration_mismatch"))

    def test_derivative_validation_requires_trusted_positive_duration(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            target = module.STAGING / "untrusted-duration"
            target.mkdir()
            for duration in (None, 0):
                with self.subTest(duration=duration), self.assertRaisesRegex(
                    ValueError, "derivative_duration_unavailable"
                ):
                    module._validate_derivative(target, duration)
            for duration in (float("nan"), float("inf"), -1):
                with self.subTest(duration=duration), self.assertRaisesRegex(
                    ValueError, "derivative_duration_invalid"
                ):
                    module._validate_derivative(target, duration)

    def test_playlist_rejects_duplicate_pending_ranges_and_tag_uris(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            duplicate = (
                '#EXTM3U\n#EXT-X-MAP:URI="index.m4s",BYTERANGE="100@0"\n'
                '#EXT-X-BYTERANGE:100@100\n#EXT-X-BYTERANGE:100@300\n'
                '#EXTINF:20,\nindex.m4s\n#EXT-X-ENDLIST\n'
            )
            with self.assertRaisesRegex(ValueError, "playlist_invalid_range"):
                module._ranges(duplicate, 1000)
            external_key = (
                '#EXTM3U\n#EXT-X-MAP:URI="index.m4s",BYTERANGE="100@0"\n'
                '#EXT-X-KEY:METHOD=AES-128,URI="https://example.test/key"\n'
                '#EXT-X-BYTERANGE:100@100\n#EXTINF:20,\nindex.m4s\n#EXT-X-ENDLIST\n'
            )
            with self.assertRaisesRegex(ValueError, "playlist_external_uri"):
                module._ranges(external_key, 1000)
            duplicate_map_attribute = (
                '#EXTM3U\n#EXT-X-MAP:URI="index.m4s",BYTERANGE="100@0",URI="https://example.test/other"\n'
                '#EXTINF:20,\n#EXT-X-BYTERANGE:100@100\nindex.m4s\n#EXT-X-ENDLIST\n'
            )
            with self.assertRaisesRegex(ValueError, "playlist_invalid_map"):
                module._ranges(duplicate_map_attribute, 1000)

    def test_derivative_requires_determinate_duration_and_bitrate(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            target = module.STAGING / "validation"
            target.mkdir()
            media = target / "index.m4s"
            media.write_bytes(b"x" * 2048)
            (target / "index.m3u8").write_text(
                '#EXTM3U\n#EXT-X-MAP:URI="index.m4s",BYTERANGE="100@0"\n'
                '#EXT-X-BYTERANGE:1948@100\n#EXTINF:20,\nindex.m4s\n#EXT-X-ENDLIST\n',
                encoding="utf-8",
            )
            with patch.object(module, "_probe", return_value={"codec": "aac", "channels": 1, "duration": 0, "bit_rate": 0}):
                with self.assertRaisesRegex(ValueError, "derivative_duration_invalid"):
                    module._validate_derivative(target, 100, 1)
            media.write_bytes(b"x" * (1024 * 1024))
            with patch.object(module, "_probe", return_value={"codec": "aac", "channels": 1, "duration": 1, "bit_rate": 0}):
                with self.assertRaisesRegex(ValueError, "derivative_bitrate_invalid"):
                    module._validate_derivative(target, 1, 1)

    def test_probe_rejects_nonfinite_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            payload = json.dumps({"streams": [{"codec_name": "aac", "channels": 2}], "format": {"duration": "NaN"}})
            with patch.object(module.subprocess, "run", return_value=SimpleNamespace(stdout=payload)):
                with self.assertRaisesRegex(ValueError, "audio_probe_invalid"):
                    module._probe(Path(temporary) / "source.m4b")

    def test_preparer_mounts_only_originals_and_rebuildable_writable_state(self):
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        worker = compose.split("  audiobook-preparer:\n", 1)[1].split(
            "  mytube-preparer:\n", 1
        )[0]
        originals = worker.split(
            "source: ${DAVID_PI_DATA_ROOT:-/srv/david-pi/data}/audiobooks/originals\n", 1
        )[1].split("      - type: bind", 1)[0]
        self.assertIn("target: /data/audiobooks/originals", originals)
        self.assertIn("read_only: true", originals)
        self.assertNotIn("source: ${DAVID_PI_DATA_ROOT:-/srv/david-pi/data}/audiobooks\n", worker)
        self.assertNotIn("/audiobooks/covers", worker)
        self.assertNotIn("/audiobooks/trash", worker)
        self.assertNotIn("/audiobooks/playback", worker)
        for source, target in (
            ("audiobooks/streaming", "/data/audiobooks/streaming"),
            ("audiobooks/incoming/streaming", "/data/audiobooks/incoming/streaming"),
            (".david-pi-operations/audiobook", "/audiobook-state"),
        ):
            mount = worker.split(f"source: ${{DAVID_PI_DATA_ROOT:-/srv/david-pi/data}}/{source}\n", 1)[1].split(
                "      - type: bind", 1
            )[0]
            self.assertIn(f"target: {target}", mount)
            self.assertNotIn("read_only: true", mount)
        self.assertIn("DAVID_PI_AUDIOBOOK_DERIVATIVE_STATE: /audiobook-state", worker)
        self.assertNotIn("DAVID_PI_PLATFORM_DATA", worker)
        self.assertIn("network_mode: none", worker)
        self.assertIn('command: ["python", "-m", "modules.audiobook_prepare_worker"]', worker)

        with tempfile.TemporaryDirectory() as temporary:
            module = load_isolated_streaming(temporary)
            module.reconcile_catalog([])
            with patch.object(module, "system_pause_reason", return_value=None):
                self.assertEqual(module.work_once(), {"state": "idle"})

        entrypoint = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
        audiobook_guard = entrypoint.split(
            'DAVID_PI_WORKER_MODE:-}" = "audiobook"', 1
        )[1].split('DAVID_PI_WORKER_MODE:-}" = "maintenance"', 1)[0]
        self.assertIn('[ -w /data/audiobooks ]', audiobook_guard)
        self.assertIn('mount_is_read_only /data/audiobooks/originals', audiobook_guard)
        self.assertIn('for path in "$audiobook_state"', audiobook_guard)
        self.assertIn('[ -L "$path" ]', audiobook_guard)

        unit = (ROOT / "deploy" / "david-pi-portal.service").read_text(encoding="utf-8")
        self.assertIn("ConditionPathIsDirectory=/srv/data/family-photos/audiobooks/originals", unit)
        # Systemd conditions run before the create-only helper, so rebuildable
        # targets must not be startup conditions or shell-created paths.
        self.assertNotIn("ConditionPathIsDirectory=/srv/data/family-photos/.david-pi-operations/audiobook", unit)
        self.assertNotIn("ConditionPathIsDirectory=/srv/data/family-photos/audiobooks/streaming", unit)
        self.assertNotIn("ConditionPathIsDirectory=/srv/data/family-photos/audiobooks/incoming/streaming", unit)
        self.assertNotIn("install -d", unit)

        preparer = load_state_preparer()
        with tempfile.TemporaryDirectory() as temporary:
            anchor = Path(temporary)
            anchor.chmod(0o700)
            data = anchor / "family-photos"
            data.mkdir(mode=0o755)
            sentinel = data / preparer.SENTINEL_NAME
            sentinel.write_bytes(preparer.SENTINEL_VALUE + b"\n")
            sentinel.chmod(0o644)
            audiobook_root = data / preparer.AUDIOBOOK_ROOT_NAME
            audiobook_root.mkdir(mode=0o755)
            originals = audiobook_root / preparer.AUDIOBOOK_ORIGINALS_NAME
            originals.mkdir(mode=0o755)
            original_marker = originals / "must-not-be-touched.m4b"
            original_marker.write_bytes(b"original-audio-evidence")
            incoming = audiobook_root / preparer.AUDIOBOOK_INCOMING_NAME
            incoming.mkdir(mode=0o755)
            expected_chain = (
                (anchor, os.getuid(), os.getgid(), 0o700),
                (data, os.getuid(), os.getgid(), 0o755),
            )
            common = {
                "operations_uid": os.getuid(),
                "operations_gid": os.getgid(),
                "sentinel_uid": os.getuid(),
                "sentinel_gid": os.getgid(),
                "expected_chain": expected_chain,
                "require_root": False,
                "writer_check": lambda: None,
            }
            preparer.prepare_state(
                data,
                maintenance_uid=os.getuid(),
                maintenance_gid=os.getgid(),
                **common,
            )
            marker_details = original_marker.stat()
            before = (
                marker_details.st_dev,
                marker_details.st_ino,
                marker_details.st_uid,
                marker_details.st_gid,
                marker_details.st_mode,
                marker_details.st_size,
                marker_details.st_mtime_ns,
                marker_details.st_ctime_ns,
                original_marker.read_bytes(),
            )
            preparer.prepare_audiobook_state(
                data,
                audiobook_uid=os.getuid(),
                audiobook_gid=os.getgid(),
                **common,
            )
            targets = (
                data / preparer.OPERATIONS_NAME / preparer.AUDIOBOOK_STATE_NAME,
                audiobook_root / preparer.AUDIOBOOK_STREAMING_NAME,
                incoming / preparer.AUDIOBOOK_STREAMING_NAME,
            )
            self.assertTrue(all(path.is_dir() for path in targets))
            self.assertTrue(
                all((path.stat().st_mode & 0o777) == 0o750 for path in targets)
            )
            marker_details = original_marker.stat()
            after = (
                marker_details.st_dev,
                marker_details.st_ino,
                marker_details.st_uid,
                marker_details.st_gid,
                marker_details.st_mode,
                marker_details.st_size,
                marker_details.st_mtime_ns,
                marker_details.st_ctime_ns,
                original_marker.read_bytes(),
            )
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
