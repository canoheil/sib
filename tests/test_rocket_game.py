"""Apple Rocket service, migration and Telegram animation tests."""

import importlib.util
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image
import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sib.database.models import (
    AppleTransaction,
    AppleWallet,
    SystemConfig,
    User,
    UserRocketState,
)
from sib.handlers import rocket as rocket_handler
from sib.services import rocket_service
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import CONFIG_DEFAULTS

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "database/migrations/versions/075_rocket_game.py"
EXPECTED_CONFIG = {
    "rocket_min_bet": 50,
    "rocket_max_bet": 5000,
    "rocket_cooldown_seconds": 30,
    "rocket_house_edge": 5,
}
EXPECTED_SEED = {key: str(value) for key, value in EXPECTED_CONFIG.items()}


def _migration():
    spec = importlib.util.spec_from_file_location("migration_075_rocket_game", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _sqlite_integer_transaction_pk():
    column = AppleTransaction.__table__.c.id
    original = column.type
    column.type = sa.Integer()
    try:
        yield
    finally:
        column.type = original


def _foreign_keys_on(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sa.event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    tables = [
        User.__table__,
        SystemConfig.__table__,
        AppleWallet.__table__,
        AppleTransaction.__table__,
        UserRocketState.__table__,
    ]
    with _sqlite_integer_transaction_pk():
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: User.metadata.create_all(
                    sync_connection, tables=tables
                )
            )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as test_session:
        yield test_session
    await engine.dispose()


@pytest.fixture
def fixed_db_now(monkeypatch):
    moment = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

    async def database_now(_session):
        return moment

    monkeypatch.setattr(rocket_service, "_database_now", database_now)
    return moment


async def _seed_player(session, user_id=1, balance=1000):
    session.add(User(id=user_id, first_name="Rocket Player"))
    session.add(AppleWallet(user_id=user_id, balance=balance))
    await session.flush()


def test_crash_point_rng_formula_is_mockable():
    assert rocket_service.generate_crash_point(5, rng=lambda: 0.049) == 1.0
    assert rocket_service.generate_crash_point(5, rng=lambda: 0.5) == pytest.approx(1.9)
    assert rocket_service.generate_crash_point(5, rng=lambda: 0.6) == 2.3
    assert rocket_service.generate_crash_point(5, rng=lambda: 0.9999) == 100.0
    assert rocket_service.generate_crash_point(0, rng=lambda: 0.5) == 2.0


@pytest.mark.parametrize(
    ("is_win", "image_name"),
    [(True, "rocket_win.jpg"), (False, "rocket_boom.jpg")],
)
def test_generate_boom_image_returns_a_valid_jpeg(is_win, image_name):
    opened = []
    real_open = Image.open

    def recording_open(path, *args, **kwargs):
        opened.append(Path(path).name)
        return real_open(path, *args, **kwargs)

    with mock.patch.object(rocket_service.Image, "open", recording_open):
        data = rocket_service.generate_boom_image(22.3, is_win=is_win)

    assert opened == [image_name]
    assert isinstance(data, bytes)
    with Image.open(BytesIO(data)) as image:
        assert image.format == "JPEG"
        image.verify()


@pytest.mark.asyncio
async def test_play_debits_bet_and_enforces_cooldown(
    session, fixed_db_now, monkeypatch
):
    await _seed_player(session, balance=1000)
    monkeypatch.setattr(rocket_service, "generate_crash_point", lambda _edge: 2.5)

    first = await rocket_service.play_rocket(session, 1, Decimal("2.5"), 200)
    assert first["status"] == "started"
    assert first["crash_point"] == 2.5
    assert first["target"] == 2.5
    assert first["bet"] == 200
    assert await AppleWalletService.get_balance(session, 1) == 800
    state = await session.get(UserRocketState, 1)
    assert state.last_played_at.replace(tzinfo=timezone.utc) == fixed_db_now

    second = await rocket_service.play_rocket(session, 1, 2.0, 200)
    assert second == {"status": "cooldown", "remaining_seconds": 30}
    assert await AppleWalletService.get_balance(session, 1) == 800

    transactions = list(
        (await session.scalars(sa.select(AppleTransaction))).all()
    )
    assert len(transactions) == 1
    assert transactions[0].amount == -200
    assert transactions[0].reason == rocket_service.BET_REASON


@pytest.mark.asyncio
async def test_win_payout_is_credited_once(session, monkeypatch):
    await _seed_player(session, balance=1000)
    monkeypatch.setattr(rocket_service, "generate_crash_point", lambda _edge: 2.5)
    game = await rocket_service.play_rocket(session, 1, 2.5, 200)

    result = await rocket_service.settle_rocket(
        session,
        1,
        game_id=game["game_id"],
        bet_amount=game["bet"],
        target_multiplier=game["target"],
        crash_point=game["crash_point"],
    )
    replay = await rocket_service.settle_rocket(
        session,
        1,
        game_id=game["game_id"],
        bet_amount=game["bet"],
        target_multiplier=game["target"],
        crash_point=game["crash_point"],
    )

    assert result["status"] == "win"
    assert result["payout"] == 500
    assert replay["payout"] == 500
    assert await AppleWalletService.get_balance(session, 1) == 1300
    payouts = list(
        (
            await session.scalars(
                sa.select(AppleTransaction).where(
                    AppleTransaction.reason == rocket_service.PAYOUT_REASON
                )
            )
        ).all()
    )
    assert len(payouts) == 1


@pytest.mark.asyncio
async def test_loss_keeps_the_deducted_bet_without_a_payout(session, monkeypatch):
    await _seed_player(session, balance=1000)
    monkeypatch.setattr(rocket_service, "generate_crash_point", lambda _edge: 2.4)
    game = await rocket_service.play_rocket(session, 1, 2.5, 200)

    result = await rocket_service.settle_rocket(
        session,
        1,
        game_id=game["game_id"],
        bet_amount=game["bet"],
        target_multiplier=game["target"],
        crash_point=game["crash_point"],
    )

    assert result == {"status": "loss", "payout": 0, "crash_point": 2.4}
    assert await AppleWalletService.get_balance(session, 1) == 800
    assert not list(
        (
            await session.scalars(
                sa.select(AppleTransaction).where(
                    AppleTransaction.reason == rocket_service.PAYOUT_REASON
                )
            )
        ).all()
    )


@pytest.mark.asyncio
async def test_play_validates_target_bet_and_balance(session, monkeypatch):
    await _seed_player(session, balance=100)
    monkeypatch.setattr(rocket_service, "generate_crash_point", lambda _edge: 2.0)

    assert (await rocket_service.play_rocket(session, 1, 1.09, 50))["status"] == "invalid_target"
    assert (await rocket_service.play_rocket(session, 1, 2.0, 49))["status"] == "invalid_bet"
    insufficient = await rocket_service.play_rocket(session, 1, 2.0, 200)
    assert insufficient["status"] == "insufficient_funds"
    assert await AppleWalletService.get_balance(session, 1) == 100


def test_rocket_command_regex_parsing():
    assert rocket_handler.parse_rocket_command("سیب 2.5 200") == (Decimal("2.5"), 200)
    assert rocket_handler.parse_rocket_command("سیب 1..5 200") is None
    assert rocket_handler.parse_rocket_command("سیب 2.5 200.0") is None
    assert rocket_handler.parse_rocket_command(" سیب 2.5 200") is None
    assert rocket_handler.parse_rocket_command("سیب 2.5") is None


@pytest.mark.asyncio
async def test_handler_ignores_private_chats_and_replies():
    class MessageStub:
        def __init__(self, chat_type, reply_to_message=None):
            self.chat = SimpleNamespace(type=chat_type)
            self.reply_to_message = reply_to_message
            self.from_user = SimpleNamespace(id=7, is_bot=False)
            self.text = "سیب 2.5 200"
            self.replies = []

        async def reply(self, *args, **kwargs):
            self.replies.append((args, kwargs))

    private = MessageStub("private")
    group_reply = MessageStub("group", reply_to_message=object())

    await rocket_handler.handle_rocket(private, bot=object())
    await rocket_handler.handle_rocket(group_reply, bot=object())

    assert private.replies == []
    assert group_reply.replies == []


@pytest.mark.asyncio
async def test_animation_edits_caption_and_replaces_launch_animation_with_boom_photo(
    monkeypatch,
):
    class BotStub:
        def __init__(self):
            self.captions = []
            self.media = []

        async def edit_message_caption(self, **kwargs):
            self.captions.append(kwargs)

        async def edit_message_media(self, **kwargs):
            self.media.append(kwargs)

    bot = BotStub()
    delays = []

    async def no_wait(seconds):
        delays.append(seconds)

    async def settle(_user_id, _game):
        return {"status": "win", "payout": 500}

    monkeypatch.setattr(rocket_handler, "_settle_round", settle)
    game = {
        "game_id": "test-game",
        "bet": 200,
        "target": 2.5,
        "crash_point": 3.0,
    }
    caption = await rocket_handler.run_rocket_animation(
        bot,
        chat_id=-123,
        message_id=45,
        user_id=7,
        game=game,
        sleep=no_wait,
    )

    assert len(delays) == len(bot.captions) == 4
    assert all(delay == 1 for delay in delays)
    assert len(bot.media) == 1
    assert "در حال اوج‌گیری" in bot.captions[0]["caption"]
    assert bot.captions[-1]["caption"].endswith("(3.0x)")
    assert bot.media[0]["chat_id"] == -123
    assert bot.media[0]["message_id"] == 45
    assert bot.media[0]["media"].caption == caption
    assert bot.media[0]["media"].media.filename == "rocket_win.jpg"
    assert "در 3.0x ترکید" in caption
    assert "(+500 سیب)" in caption


@pytest.mark.asyncio
async def test_deleted_message_still_settles_without_more_edits(monkeypatch):
    class DeletedMessageBot:
        def __init__(self):
            self.caption_edits = 0
            self.media_edits = 0
            self.final_messages = []

        async def edit_message_caption(self, **_kwargs):
            self.caption_edits += 1
            raise RuntimeError("message to edit not found")

        async def edit_message_media(self, **_kwargs):
            self.media_edits += 1
            raise AssertionError("do not retry an edit after the message was deleted")

        async def send_message(self, **kwargs):
            self.final_messages.append(kwargs)

    bot = DeletedMessageBot()
    settled = []

    async def settle(user_id, game):
        settled.append((user_id, game["game_id"]))
        return {"status": "loss", "payout": 0}

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(rocket_handler, "_settle_round", settle)
    caption = await rocket_handler.run_rocket_animation(
        bot,
        chat_id=-123,
        message_id=45,
        user_id=7,
        game={"game_id": "gone", "bet": 50, "target": 2.0, "crash_point": 1.5},
        sleep=no_wait,
    )

    assert bot.caption_edits == 1
    assert bot.media_edits == 0
    assert settled == [(7, "gone")]
    assert bot.final_messages[0]["text"] == caption
    assert "شما باختید" in caption


def test_075_migration_revises_074_and_077_is_the_single_head():
    migration = _migration()
    assert migration.revision == "075_rocket_game"
    assert migration.down_revision == "074_theft_system"
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["077_pet_rename"]
    assert scripts.get_revision("075_rocket_game").down_revision == "074_theft_system"
    assert {key: CONFIG_DEFAULTS[key] for key in EXPECTED_CONFIG} == EXPECTED_CONFIG
    assert migration._CONFIG_DEFAULTS == EXPECTED_SEED


def test_075_migration_creates_state_seeds_and_preserves_overrides():
    engine = sa.create_engine("sqlite:///:memory:")
    migration = _migration()
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE users (id BIGINT PRIMARY KEY)"))
            connection.execute(
                sa.text(
                    "CREATE TABLE system_config ("
                    "`key` VARCHAR(64) PRIMARY KEY, `value` TEXT NOT NULL)"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO system_config (`key`, `value`) "
                    "VALUES ('rocket_house_edge', '7'), ('other', 'preserve')"
                )
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.upgrade()
            inspector = sa.inspect(connection)
            assert inspector.has_table("user_rocket_states")
            columns = {
                column["name"]: column
                for column in inspector.get_columns("user_rocket_states")
            }
            assert set(columns) == {"user_id", "last_played_at"}
            assert not columns["user_id"]["nullable"]
            assert not columns["last_played_at"]["nullable"]
            foreign_key = inspector.get_foreign_keys("user_rocket_states")[0]
            assert foreign_key["referred_table"] == "users"
            assert foreign_key["options"]["ondelete"] == "CASCADE"
            config = dict(
                connection.execute(sa.text("SELECT `key`, `value` FROM system_config")).all()
            )
            assert config == {**EXPECTED_SEED, "rocket_house_edge": "7", "other": "preserve"}

            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                migration.downgrade()
            assert not sa.inspect(connection).has_table("user_rocket_states")
            config = dict(
                connection.execute(sa.text("SELECT `key`, `value` FROM system_config")).all()
            )
            assert config == {"rocket_house_edge": "7", "other": "preserve"}
    finally:
        engine.dispose()


def test_alembic_upgrade_from_074_and_downgrade(tmp_path):
    database = tmp_path / "rocket_migration.db"
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE users (id BIGINT PRIMARY KEY)"))
        connection.execute(
            sa.text(
                "CREATE TABLE system_config (`key` VARCHAR(64) PRIMARY KEY, "
                "`value` TEXT NOT NULL)"
            )
        )
    engine.dispose()

    env = os.environ.copy()
    env["DATABASE_SYNC_URL"] = f"sqlite:///{database}"
    env["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT.parent), ROOT.as_posix(), env.get("PYTHONPATH", ""))
    )

    def alembic(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    alembic("stamp", "074_theft_system")
    alembic("upgrade", "head")
    assert alembic("heads") == "077_pet_rename (head)"
    assert "077_pet_rename" in alembic("current")
    engine = sa.create_engine(f"sqlite:///{database}")
    assert sa.inspect(engine).has_table("user_rocket_states")
    # Later migrations seed their own keys, so assert only rocket's values.
    rows = dict(
        engine.connect().execute(
            sa.text("SELECT `key`, `value` FROM system_config")
        ).all()
    )
    assert {key: rows[key] for key in EXPECTED_SEED} == EXPECTED_SEED
    engine.dispose()

    alembic("downgrade", "074_theft_system")
    engine = sa.create_engine(f"sqlite:///{database}")
    assert not sa.inspect(engine).has_table("user_rocket_states")
    assert engine.connect().execute(sa.text("SELECT COUNT(*) FROM system_config")).scalar_one() == 0
    engine.dispose()
