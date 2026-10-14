"""Phase 8C defaults, conditional migration, narrative context and caller wiring."""
import importlib.util
from pathlib import Path
from random import Random
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, select

from sib.database.models import Base, Inv2Attack, Inv2Reward, PlayerCard, SystemConfig
from sib.handlers import inv2_join
from sib.services.config_store import ConfigService, CONFIG_DEFAULTS, DEFAULT_INVASION_DIFFICULTY_MULTIPLIER
from sib.services.invasion_v2 import narrative as n, narrative_pool as pool, presentation
from sib.tests.invasion_v2.test_presentation_phase5 import db, create_inv, callback, NOW

ROOT = Path(__file__).resolve().parents[2]
COMMON = dict(username="داریوش", card_name="سیب سرخ", region_name="درّه ماه", enemy_name="شیاسیب سایه")
ATTACK = dict(username="داریوش", card_name="سیب سرخ", damage=123, enemy_hp_remaining=456)
CASES = {
    "announcement": (n.render_announcement, COMMON, "announcement"),
    "normal": (n.render_join, {**ATTACK, "is_crit": False}, "join_normal"),
    "critical": (n.render_join, {**ATTACK, "is_crit": True}, "join_crit"),
    "kill": (n.render_join, {**ATTACK, "is_crit": True, "enemy_hp_remaining": 0}, "join_kill"),
    "high": (n.render_high_damage, ATTACK, "join_crit"),
    "high_kill": (n.render_high_damage, {**ATTACK, "enemy_hp_remaining": -1}, "join_kill"),
    "finisher": (n.render_finisher, COMMON, "join_kill"),
    "victory": (n.render_victory, COMMON, "victory"),
    "counted_victory": (n.render_victory, {**COMMON, "participant_count": 17}, "victory"),
    "defeat": (n.render_defeat, COMMON, "defeat"),
    "expired": (n.render_expired, COMMON, "expired"),
}


@pytest.fixture(autouse=True)
def reset_history():
    with pool._lock:
        pool._history.clear()
    yield
    with pool._lock:
        pool._history.clear()


def test_difficulty_default_is_2_5():
    assert DEFAULT_INVASION_DIFFICULTY_MULTIPLIER == 2.5
    assert CONFIG_DEFAULTS["invasion_difficulty_multiplier"] == 2.5
    assert ConfigService.default("invasion_difficulty_multiplier") == 2.5


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,expected", [(None, 2.5), (3.0, 3.0), (4.0, 4.0), (2.5, 2.5)])
async def test_config_default_never_overwrites_stored_admin_value(db, stored, expected):
    factory, _ = db
    if stored is not None:
        async with factory.begin() as session:
            await ConfigService.set(session, "invasion_difficulty_multiplier", stored)
    async with factory() as session:
        assert await ConfigService.get(session, "invasion_difficulty_multiplier") == expected
        if stored is None:
            assert await session.get(SystemConfig, "invasion_difficulty_multiplier") is None


def _migration():
    path = ROOT / "database/migrations/versions/040_inv2_difficulty_default.py"
    spec = importlib.util.spec_from_file_location("migration_040", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("value,expected", [("4.0", "2.5"), ("3.0", "3.0"), ("2.5", "2.5"),
                                           ("4", "4"), ("4.00", "4.00"), (None, None)])
def test_migration_040_updates_only_default(value, expected):
    migration = _migration()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        Base.metadata.create_all(connection, tables=[SystemConfig.__table__])
        connection.execute(SystemConfig.__table__.insert(), [
            {"key": "invasion_enabled", "value": "false"},
            {"key": "unrelated", "value": "4.0"}])
        if value is not None:
            connection.execute(SystemConfig.__table__.insert(),
                {"key": "invasion_difficulty_multiplier", "value": value})
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()  # Idempotent, and missing rows remain missing.
        values = dict(connection.execute(select(SystemConfig.key, SystemConfig.value)).all())
        assert values.get("invasion_difficulty_multiplier") == expected
        assert values["invasion_enabled"] == "false" and values["unrelated"] == "4.0"
    engine.dispose()


@pytest.mark.parametrize("value,expected", [("2.5", "4.0"), ("3.0", "3.0"), ("4.0", "4.0"), (None, None)])
def test_migration_040_downgrade_only_new_default(value, expected):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        Base.metadata.create_all(connection, tables=[SystemConfig.__table__])
        connection.execute(SystemConfig.__table__.insert(), {"key": "invasion_enabled", "value": "false"})
        if value is not None:
            connection.execute(SystemConfig.__table__.insert(),
                {"key": "invasion_difficulty_multiplier", "value": value})
        with Operations.context(MigrationContext.configure(connection)):
            _migration().downgrade()
            _migration().downgrade()
        values = dict(connection.execute(select(SystemConfig.key, SystemConfig.value)).all())
        assert values.get("invasion_difficulty_multiplier") == expected
        assert values["invasion_enabled"] == "false"
    engine.dispose()


def test_migration_chain_head():
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "database/migrations"))
    scripts = ScriptDirectory.from_config(cfg)
    assert scripts.get_current_head() == "042_inv2_pinned_announcement"
    assert _migration().down_revision == "039_archive_inv1_tables"
    chain = {"041_inv2_announcement_narrative": "040_inv2_difficulty_default",
             "042_inv2_pinned_announcement": "041_inv2_announcement_narrative"}
    for revision, parent in chain.items():
        path = ROOT / f"database/migrations/versions/{revision}.py"
        spec = importlib.util.spec_from_file_location(f"migration_{revision}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.down_revision == parent


@pytest.mark.parametrize("case", CASES)
def test_render_with_invasion_id_uses_it_as_key(monkeypatch, case):
    render, values, expected_event = CASES[case]
    spy = Mock(wraps=pool.pick)
    monkeypatch.setattr(pool, "pick", spy)
    recent = []
    for _ in range(20):
        text = render(**values, rng=Random(17), invasion_id=123)
        assert text not in recent[-5:]
        recent.append(text)
        assert spy.call_args.kwargs["key"] == f"{expected_event}:123"
    # Separate IDs start independent histories even with identical values/RNG.
    other = render(**values, rng=Random(17), invasion_id=124)
    assert other == recent[0]
    render(**values, rng=Random(17), invasion_id=0)
    assert spy.call_args.kwargs["key"] == f"{expected_event}:0"


@pytest.mark.parametrize("case", CASES)
def test_render_without_invasion_id_falls_back(monkeypatch, case):
    import hashlib
    import json
    render, values, event_name = CASES[case]
    spy = Mock(wraps=pool.pick)
    monkeypatch.setattr(pool, "pick", spy)
    render(**values, rng=Random(19))
    omitted = spy.call_args.kwargs["key"]
    text = render(**values, rng=Random(19), invasion_id=None)
    assert text and omitted == spy.call_args.kwargs["key"]
    if event_name.startswith("join_"):
        prefix, context = "join", (values["username"], values["card_name"])
    else:
        prefix = "announce" if event_name == "announcement" else event_name
        context = (values["region_name"], values["enemy_name"])
    digest = hashlib.sha256(json.dumps(context, ensure_ascii=False).encode()).hexdigest()
    assert omitted == f"{prefix}:{digest}"


@pytest.mark.parametrize("count", [0, 1, 7, 12, 12345])
def test_render_victory_with_participant_count(count):
    digits = str(count).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
    expected = {text.format(user=COMMON["username"], card=COMMON["card_name"],
        region=COMMON["region_name"], enemy_name=COMMON["enemy_name"], n=digits)
        for text in pool.VICTORY if "{n}" in text}
    for seed in range(18):
        text = n.render_victory(**COMMON, rng=Random(seed), invasion_id=22, participant_count=count)
        assert digits in text and text in expected
        assert "{" not in text and len(text) < 900


def test_render_victory_without_participant_count():
    expected = {text.format(user=COMMON["username"], card=COMMON["card_name"],
        region=COMMON["region_name"], enemy_name=COMMON["enemy_name"])
        for text in pool.VICTORY if "{n}" not in text}
    for seed in range(30):
        text = n.render_victory(**COMMON, rng=Random(seed), invasion_id=22)
        assert text in expected and "{n}" not in text and "None" not in text


@pytest.mark.parametrize("case", CASES)
def test_no_placeholder_leaks(case):
    render, arguments, _ = CASES[case]
    for seed in range(15):
        values = {**arguments, "username": "داریوش" * (1 if seed % 2 else 200),
                  "card_name": "سیب سرخ" * (1 if seed % 2 else 200)}
        if "region_name" in values:
            values["region_name"] = "درّه ماه" * (1 if seed % 2 else 200)
        text = render(**values, rng=Random(seed), invasion_id=None if seed % 3 else seed)
        assert "{" not in text and "}" not in text and 0 < len(text) < 900


@pytest.mark.asyncio
async def test_announcement_passes_invasion_id(db, monkeypatch):
    factory, _ = db
    inv_id = await create_inv(factory)
    spy = Mock(wraps=n.render_announcement)
    monkeypatch.setattr(n, "render_announcement", spy)
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(
        chat=SimpleNamespace(id=-100), message_id=123)))
    await presentation.InvasionPresentation().announce(bot, inv_id)
    assert spy.call_args.kwargs["invasion_id"] == inv_id
    bot.send_photo.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["VICTORY", "DEFEAT"])
async def test_finalization_wires_id_and_counts_only_this_invasion_once(db, monkeypatch, status):
    factory, _ = db
    inv_id = await create_inv(factory, status=status, message_id=123)
    other_id = await create_inv(factory, status="ACTIVE", hp=400, message_id=124)
    async with factory.begin() as session:
        session.add_all([PlayerCard(id=101, user_id=1, character_id=10, level=1, is_favorite=True),
                         PlayerCard(id=102, user_id=2, character_id=10, level=1, is_favorite=True)])
        for invasion, user, card in [(inv_id, 1, 101), (inv_id, 2, 102), (other_id, 1, 101)]:
            session.add(Inv2Attack(invasion_id=invasion, user_id=user, card_id=card, card_power=20,
                                  damage=20, is_crit=False, narrative="نگهبان ایستاد.", joined_at=NOW))
        for kind in ("golden_apple", "diamond_apple", "obsidian_apple"):
            session.add(Inv2Reward(invasion_id=inv_id, user_id=1, reward_type=kind, reward_amount=1, granted_at=NOW))
    name = "render_victory" if status == "VICTORY" else "render_defeat"
    spy = Mock(wraps=getattr(n, name))
    monkeypatch.setattr(n, name, spy)
    counts = []
    def observe(conn, cursor, statement, parameters, context, executemany):
        if "count(" in statement.lower() and "inv2_attacks" in statement.lower():
            counts.append(statement)
    engine = factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", observe)
    bot = SimpleNamespace(edit_message_caption=AsyncMock(return_value=True), send_photo=AsyncMock(return_value=True))
    try:
        assert await presentation.InvasionPresentation().finalize(bot, inv_id)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert spy.call_args.kwargs["invasion_id"] == inv_id
    if status == "VICTORY":
        assert spy.call_args.kwargs["participant_count"] == 2  # Not 3 attacks or 3 reward rows.
        assert len(counts) == 1
        assert "۲" in bot.edit_message_caption.await_args.kwargs["caption"]
    else:
        assert "participant_count" not in spy.call_args.kwargs and counts == []
    bot.edit_message_caption.assert_awaited_once()
    bot.send_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_join_passes_invasion_id_without_changing_damage_flow(db, monkeypatch):
    import asyncio
    factory, _ = db
    inv_id = await create_inv(factory, status="ACTIVE", hp=500, message_id=123)
    async with factory.begin() as session:
        session.add(PlayerCard(id=101, user_id=1, character_id=10, level=1, is_favorite=True))
    spy = Mock(wraps=inv2_join.render_join)
    monkeypatch.setattr(inv2_join, "render_join", spy)
    monkeypatch.setattr(inv2_join, "_answer", AsyncMock())
    delivered = asyncio.Event()
    async def on_join(*args):
        delivered.set()
    monkeypatch.setattr(presentation.InvasionPresentation, "on_join", on_join)
    await inv2_join.handle_inv2_join(callback(inv_id), SimpleNamespace())
    await asyncio.wait_for(delivered.wait(), timeout=5)
    spy.assert_called_once()
    assert spy.call_args.kwargs["invasion_id"] == inv_id
    async with factory() as session:
        attack = await session.scalar(select(Inv2Attack).where(Inv2Attack.invasion_id == inv_id))
        assert attack.damage == spy.call_args.kwargs["damage"]
        assert spy.call_args.kwargs["enemy_hp_remaining"] == 500 - attack.damage


# Phase 8B pool fingerprints: existing prose must remain byte-for-byte intact.
# ANNOUNCEMENT was refreshed once, in the Persian copy audit, for the compound
# spelling «چشم‌به‌راه» (was «چشم به راه»); every other pool is still the original.
ORIGINAL_POOLS = {'ANNOUNCEMENT': (12, '8c057889fcb4c0ac8ff6a0c74d6d44d3f85fbf3bee454a3366e30d992cd48ed7'), 'JOIN_NORMAL': (22, '1ff97177b187cdbe5923cec9b73b4fdc9d966f3826cdc0dcb8b9aafe7dded901'), 'JOIN_CRIT': (16, '8c14dda3f60db2c2ae35c1755232964bb201e5cd12b6401f54c0d77d72608464'), 'JOIN_KILL': (10, '3fa197a29837874239167f254cd3ff37f024d5306c628005c1ea2e976d1ee653'), 'VICTORY': (12, '41cb3d57b7d062e8dca38436fcb95d86e6793c561d67dd3272bbf6719d07484a'), 'DEFEAT': (12, '709e4316427f80041f1a5e3d09287a5879cb915ae306ced6614b6e4f6d5d521b'), 'EXPIRED': (8, 'df0a55a077d09f23b0a083e29fcfbd98e186549a6f2d3355f9b5dab4073456f6')}

@pytest.mark.parametrize("name", ORIGINAL_POOLS)
def test_existing_narrative_variants_unchanged(name):
    import hashlib
    import json
    count, expected_hash = ORIGINAL_POOLS[name]
    items = getattr(pool, name)
    if name == "VICTORY":
        assert len(items) == 18
        assert sum("{n}" in text for text in items) == 6
        assert all("{n}" in text for text in items[count:])
    else:
        assert len(items) == count
    encoded = json.dumps(items[:count], ensure_ascii=False, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == expected_hash
