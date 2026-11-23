"""Processed light curves stay until the limit, and candidate files stay always."""
import sqlite3

from rhexas.worker.retention import (
    delete_sector,
    enforce,
    processed_limit_gb,
    usage,
    write_processed_limit_gb,
)


def _fits(cache, sector, tic, size):
    folder = cache / "data" / f"s{sector:04d}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"tess-s{sector:04d}-{tic:016d}-s_lc.fits"
    path.write_bytes(b"x" * size)
    return path


def _mark_candidate(cache, sector, tic):
    path = cache / "dashboard.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS candidates (sector INTEGER, tic INTEGER)")
    conn.execute("INSERT INTO candidates (sector, tic) VALUES (?, ?)", (sector, tic))
    conn.commit()
    conn.close()


def test_default_limit_is_20_gb_and_can_be_saved(tmp_path):
    assert processed_limit_gb(tmp_path) == 20
    assert write_processed_limit_gb(tmp_path, 2) == 2
    assert processed_limit_gb(tmp_path, 20) == 2


def test_enforce_drops_old_sectors_and_keeps_candidates(tmp_path):
    old = _fits(tmp_path, 1, 11, 800)
    kept = _fits(tmp_path, 2, 22, 800)
    _mark_candidate(tmp_path, 2, 22)
    (tmp_path / "results" / "a1" / "s0001").mkdir(parents=True)
    (tmp_path / "results" / "a1" / "s0001" / "stars.json").write_bytes(b"y" * 400)
    (tmp_path / "results" / "a1" / "s0002").mkdir(parents=True)
    (tmp_path / "results" / "a1" / "s0002" / "stars.json").write_bytes(b"z" * 400)
    # 800+800+400+400 = 2400 bytes. A 1000-byte cap must drop sector 1 only.
    outcome = enforce(tmp_path, limit_gb=1000 / 1e9)
    assert outcome["ok"] is True
    assert not old.exists()
    assert kept.exists()
    assert not (tmp_path / "results" / "a1" / "s0001").exists()
    assert (tmp_path / "results" / "a1" / "s0002").exists()
    shown = usage(tmp_path, 1000 / 1e9)
    assert shown["bytes"] == 1200
    assert any(row["sector"] == 2 and row["candidates"] == 1 for row in shown["sectors"])


def test_manual_delete_refuses_the_sector_being_scanned(tmp_path):
    path = _fits(tmp_path, 4, 44, 100)
    (tmp_path / "state.json").write_text(
        '{"sector": 4, "stage": "searching"}', encoding="utf-8",
    )
    from rhexas.worker.retention import active_sectors
    blocked = delete_sector(tmp_path, 4, active=active_sectors(tmp_path))
    assert blocked["ok"] is False
    assert blocked["error"] == "scanning"
    assert path.exists()
    (tmp_path / "state.json").write_text(
        '{"sector": 4, "stage": "uploaded"}', encoding="utf-8",
    )
    gone = delete_sector(tmp_path, 4, active=active_sectors(tmp_path))
    assert gone["ok"] is True
    assert not path.exists()
