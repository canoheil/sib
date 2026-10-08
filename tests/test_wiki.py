"""Tests for wiki & collection (Phase 12).

Wiki shows ALL released characters; collection shows ONLY owned cards.
"""

import pytest

from sib.database.models import ReleaseStatus
from sib.services.characters import CharacterService
from sib.services.player_cards import PlayerCardService
from sib.services.wiki import CollectionService, WikiService
from sib.tests.test_parties import create_test_user


async def _make_released(db_session, code, name_fa="قهرمان ویکی"):
    character = await CharacterService.create(
        db_session,
        code=code,
        name_fa=name_fa,
        power=20,
        speed=20,
        intelligence=20,
        lore_fa="یک داستان.",
        passive_key="test_passive",
        strengths=["هوش"],
        weaknesses=["سرعت"],
    )
    await CharacterService.set_release_status(db_session, character.id, ReleaseStatus.RELEASED)
    await db_session.commit()
    return character


@pytest.mark.asyncio
async def test_wiki_lists_all_released_characters(db_session):
    released = await _make_released(db_session, "wiki_a", "سیبک الف")
    draft = await CharacterService.create(
        db_session, code="wiki_draft", name_fa="پیش‌نویس", power=20, speed=20, intelligence=20
    )
    await db_session.commit()

    items = await WikiService.list_wiki(db_session)
    assert any(c.id == released.id for c in items)
    assert all(c.id != draft.id for c in items)


@pytest.mark.asyncio
async def test_wiki_page_is_persian_with_sidegrades(db_session):
    character = await _make_released(db_session, "wiki_b", "سیبک ب")
    page = await WikiService.wiki_page(db_session, character.id)
    assert page is not None
    assert "سیبک ب" in page
    assert "💪" in page and "⚡" in page and "🧠" in page
    assert "هوش" in page  # strengths
    assert "سرعت" in page  # weaknesses
    # Unimplemented raw passive keys must not be presented as active mechanics.
    assert "test_passive" not in page
    assert "توانایی منفعل" not in page

    # Unreleased characters are hidden from the wiki.
    draft = await CharacterService.create(
        db_session, code="wiki_hidden", name_fa="پنهان", power=20, speed=20, intelligence=20
    )
    await db_session.commit()
    assert await WikiService.wiki_page(db_session, draft.id) is None


@pytest.mark.asyncio
async def test_collection_shows_only_owned_cards(db_session):
    user = await create_test_user(db_session, "کالکتور ۱")
    owned = await _make_released(db_session, "wiki_c", "سیبک ج")
    not_owned = await _make_released(db_session, "wiki_d", "سیبک د")

    owned_card = await PlayerCardService.acquire(db_session, user.id, owned.id)
    await db_session.commit()

    text, total_pages = await CollectionService.render_collection(db_session, user.id)
    assert "کارت‌های من" in text
    assert "سیبک ج" in text
    assert "سیبک د" not in text
    assert total_pages == 1
    assert "تعداد کارت‌ها" in text  # summary header
    assert "کارت اصلی: انتخاب نشده" in text

    await PlayerCardService.set_favorite(
        db_session, user.id, owned_card.id, expected_main_card_id=0
    )
    await db_session.commit()
    selected_text, _ = await CollectionService.render_collection(db_session, user.id)
    assert "⭐ کارت اصلی: **سیبک ج**" in selected_text
    assert "⭐ **سیبک ج**" in selected_text

    empty_user = await create_test_user(db_session, "کالکتور ۲")
    empty_text, empty_pages = await CollectionService.render_collection(
        db_session, empty_user.id
    )
    assert "هنوز کارتی نداری" in empty_text
    assert "کارت اصلی" in empty_text
    assert empty_pages == 1


@pytest.mark.asyncio
async def test_card_detail_shows_allocation_but_no_hidden_numbers(db_session):
    user = await create_test_user(db_session, "کالکتور ۳")
    character = await _make_released(db_session, "wiki_e", "سیبک ه")
    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    await db_session.commit()

    detail = await CollectionService.render_card_detail(db_session, user.id, card.id)
    assert detail is not None
    assert "سطح `1`" in detail
    assert "💪" in detail
    assert "مجموع ویژگی‌ها" in detail
    assert "کارت اصلی هنوز انتخاب نشده" in detail
    # Hidden modifiers and sidegrade numbers stay out of the collection.
    assert "crit" not in detail.lower()
    assert "hidden" not in detail.lower()

    # Someone else's card detail is not accessible.
    thief = await create_test_user(db_session, "کالکتور ۴")
    assert await CollectionService.render_card_detail(db_session, thief.id, card.id) is None


@pytest.mark.asyncio
async def test_detail_names_a_different_current_main_card(db_session):
    user = await create_test_user(db_session, "کالکتور ۵")
    viewed_character = await _make_released(
        db_session, "wiki_viewed", "سیبکِ بازشده"
    )
    main_character = await _make_released(
        db_session, "wiki_main", "سیبکِ اصلی"
    )
    viewed_card = await PlayerCardService.acquire(
        db_session, user.id, viewed_character.id
    )
    main_card = await PlayerCardService.acquire(
        db_session, user.id, main_character.id
    )
    await db_session.commit()
    await PlayerCardService.set_favorite(
        db_session, user.id, main_card.id, expected_main_card_id=0
    )
    await db_session.commit()

    detail = await CollectionService.render_card_detail(
        db_session, user.id, viewed_card.id
    )
    assert detail is not None
    assert "کارت اصلی فعلی شما: **سیبکِ اصلی**" in detail
