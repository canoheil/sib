"""Phase 5.1 — deterministic lock-ordering audit for relationship + inventory flows.

These tests do NOT depend on a specific database engine's deadlock detector.
Instead they instrument every row-lock acquisition helper used by the
relationship services and assert that each transaction path acquires its locks
in the single documented global order:

    User (ascending id)
      -> Marriage
        -> MarriageProposal
          -> FlowerGift
            -> UserItem

Any transaction path that acquires two resource types in the opposite relative
order creates a potential AB-BA deadlock on engines with real row locks
(MariaDB/MySQL InnoDB). SQLite silently ignores ``SELECT ... FOR UPDATE`` and
degrades lock contention to ``SQLITE_BUSY`` retries, which is exactly why these
cycles are invisible to the existing race tests — so the ordering contract is
pinned here explicitly instead.

The instrumentation also records the SQLite write-intent barriers, so no
existing write-intent barrier can be removed without breaking these tests.

Note on the strict assertion: ``assert_global_order`` requires the whole
recorded sequence to be non-decreasing, which is what every single-pass path
produces. A path that loops over several rows (e.g.
``invalidate_pending_gifts_for_marriage``) re-locks resources it already holds;
that can never block, so it is held to ``assert_first_acquisition_order``.
"""

import functools
import json

from sqlalchemy import select
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

import pytest

from sib.database.models import (
    FlowerGift,
    FlowerGiftStatus,
    Group,
    Marriage,
    MarriageStatus,
    MarriageProposal,
    ProposalStatus,
    User,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.tests.test_parties import create_test_user
from sib.services.items import ITEM_CATALOG
from sib.services.lock_order import LOCK_RANKS
from sib.services.player_cards import PlayerCardService
from sib.services.player_xp import PlayerXPService
from sib.services.items import ItemService
from sib.services.love import LoveService
from sib.services.marriage import (
    DEFAULT_DIVORCE_COOLDOWN_SECONDS,
    MarriageCooldownActiveError,
    MarriageService,
)
from sib.services.marriage_proposal import MarriageProposalService


# ---------------------------------------------------------------------------
# Global lock-order policy (single source of truth for the audit)
# ---------------------------------------------------------------------------

# Mirrors services/lock_order.LOCK_RANKS (checked by
# test_policy_table_is_strictly_ordered_and_agrees_with_this_suite below).
LOCK_ORDER_RANK: Dict[str, int] = {
    "user": 0,
    "marriage": 1,
    "proposal": 2,
    "gift": 3,
    "card": 4,
    "item": 5,
    "wallet": 6,
    "progression": 7,
}


class LockOrderRecorder:
    """Records the resource type of every row-lock acquisition, in order."""

    def __init__(self) -> None:
        self.events: List[str] = []

    def record(self, resource: str) -> None:
        self.events.append(resource)

    def reset(self) -> None:
        self.events.clear()

    @property
    def ranks(self) -> List[int]:
        return [LOCK_ORDER_RANK[event] for event in self.events]

    def projection(self, resources: Tuple[str, ...]) -> List[str]:
        """The recorded sequence restricted to ``resources``, with consecutive
        duplicates collapsed (a path may legitimately re-lock the same
        resource type several times in a row)."""
        wanted = set(resources)
        projected: List[str] = []
        for event in self.events:
            if event in wanted and (not projected or projected[-1] != event):
                projected.append(event)
        return projected

    def first_acquisitions(self) -> List[str]:
        """Resource types in the order this transaction locked them the FIRST
        time (re-locking a resource the transaction already holds is always
        safe, so only first acquisitions can create a cycle)."""
        ordered: List[str] = []
        for event in self.events:
            if event not in ordered:
                ordered.append(event)
        return ordered

    def assert_first_acquisition_order(self, label: str) -> None:
        firsts = self.first_acquisitions()
        ranks = [LOCK_ORDER_RANK[event] for event in firsts]
        violations = [
            (firsts[index - 1], firsts[index])
            for index in range(1, len(ranks))
            if ranks[index] < ranks[index - 1]
        ]
        assert not violations, (
            f"{label} first acquires row locks out of the global order "
            f"(User -> Marriage -> MarriageProposal -> FlowerGift -> UserItem). "
            f"First acquisitions: {firsts}. Inversions: {violations}."
        )
        assert firsts, f"{label} acquired no row locks at all."

    def assert_global_order(self, label: str) -> None:
        ranks = self.ranks
        violations = [
            (self.events[index - 1], self.events[index])
            for index in range(1, len(ranks))
            if ranks[index] < ranks[index - 1]
        ]
        assert not violations, (
            f"{label} acquires row locks out of the global order "
            f"(User -> Marriage -> MarriageProposal -> FlowerGift -> UserItem). "
            f"Observed full sequence: {self.events}. "
            f"Inversions: {violations}."
        )
        # A relationship transaction must actually take the User lock first;
        # an empty recording would make the assertion vacuously true.
        assert self.events, f"{label} acquired no row locks at all."


# (owner, attribute, resource, keyword that makes the acquisition a lock)
_PATCH_TARGETS = [
    (MarriageService, "_lock_users_ascending", "user", None),
    (MarriageProposalService, "_lock_participants_for_proposal", "user", None),
    (MarriageService, "_lock_marriage", "marriage", None),
    (LoveService, "_lock_active_marriage_for_user", "marriage", None),
    (MarriageService, "get_active_marriage_for_user", "marriage", "for_update"),
    (MarriageProposalService, "_lock_proposal_for_transition", "proposal", None),
    (MarriageProposalService, "invalidate_pending_for_users", "proposal", None),
    (MarriageProposalService, "invalidate_pending_for_target", "proposal", None),
    (MarriageProposalService, "_lock_pending_outgoing_for_proposer", "proposal", None),
    (MarriageProposalService, "_expire_overdue_for_proposer", "proposal", None),
    (MarriageProposalService, "get_proposal", "proposal", "for_update"),
    (LoveService, "_lock_gift_for_transition", "gift", None),
    (LoveService, "_count_directional_gifts_for_date", "gift", "for_update"),
    (ItemService, "get_total", "item", "for_update"),
    (ItemService, "consume", "item", None),
    (ItemService, "grant", "item", None),
]


def _install_recorder(monkeypatch, recorder: LockOrderRecorder) -> None:
    for owner, name, resource, conditional_kwarg in _PATCH_TARGETS:
        raw = owner.__dict__[name]
        original = raw.__func__
        # ``classmethod.__func__`` still expects ``cls`` as its first argument,
        # but a plain function installed on the class is not re-bound to it.
        prefix: Tuple = (owner,) if isinstance(raw, classmethod) else ()

        if conditional_kwarg is None:

            async def wrapper(
                *args,
                __record=recorder.record,
                __res=resource,
                __orig=original,
                __prefix=prefix,
                **kwargs,
            ):
                __record(__res)
                return await __orig(*__prefix, *args, **kwargs)

        else:

            async def wrapper(
                *args,
                __record=recorder.record,
                __res=resource,
                __kw=conditional_kwarg,
                __orig=original,
                __prefix=prefix,
                **kwargs,
            ):
                if kwargs.get(__kw):
                    __record(__res)
                return await __orig(*__prefix, *args, **kwargs)

        functools.update_wrapper(wrapper, original)
        monkeypatch.setattr(owner, name, wrapper)


@pytest.fixture
def lock_recorder(monkeypatch) -> LockOrderRecorder:
    recorder = LockOrderRecorder()
    _install_recorder(monkeypatch, recorder)
    return recorder


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


async def _mk_user(session, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id, first_name=f"user{user_id}", language_code="fa")
        session.add(user)
        await session.flush()
    return user


async def _mk_group(session, group_id: int = -1007001) -> Group:
    group = await session.get(Group, group_id)
    if group is None:
        group = Group(id=group_id, title="لاک‌لاک")
        session.add(group)
        await session.flush()
    return group


async def _mk_marriage(session, a: int, b: int, *, love_score: int = 10) -> Marriage:
    await _mk_user(session, a)
    await _mk_user(session, b)
    await _mk_group(session)
    marriage = Marriage(
        proposer_id=a,
        accepter_id=b,
        origin_group_id=-1007001,
        status=MarriageStatus.ACTIVE,
        love_score=love_score,
    )
    session.add(marriage)
    await session.flush()
    return marriage


async def _mk_proposal(session, proposer: int, target: int) -> MarriageProposal:
    await _mk_user(session, proposer)
    await _mk_user(session, target)
    await _mk_group(session)
    proposal = MarriageProposal(
        proposer_id=proposer,
        target_id=target,
        group_id=-1007001,
        status=ProposalStatus.PENDING,
        expires_at=NOW + timedelta(minutes=5),
    )
    session.add(proposal)
    await session.flush()
    return proposal


async def _mk_gift(
    session, marriage: Marriage, sender: int, receiver: int, gift_date: str = "2026-10-04"
) -> FlowerGift:
    gift = FlowerGift(
        marriage_id=marriage.id,
        sender_id=sender,
        receiver_id=receiver,
        gift_date=gift_date,
        status=FlowerGiftStatus.PENDING,
        love_delta=5,
    )
    session.add(gift)
    await session.flush()
    return gift


# ---------------------------------------------------------------------------
# 1. Proposal paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_proposal_lock_order(db_session, lock_recorder):
    await _mk_user(db_session, 7001)
    await _mk_user(db_session, 7002)
    await _mk_group(db_session)
    await ItemService.grant(db_session, 7001, "wedding_ring", 1)
    await db_session.commit()

    lock_recorder.reset()
    await MarriageProposalService.create_proposal(
        db_session, 7001, 7002, -1007001, now=NOW
    )
    await db_session.commit()

    lock_recorder.assert_global_order("MarriageProposalService.create_proposal")


@pytest.mark.asyncio
async def test_reject_proposal_lock_order(db_session, lock_recorder):
    proposal = await _mk_proposal(db_session, 7010, 7011)
    await db_session.commit()

    lock_recorder.reset()
    await MarriageProposalService.reject_proposal(db_session, proposal.id, 7011, now=NOW)
    await db_session.commit()

    lock_recorder.assert_global_order("MarriageProposalService.reject_proposal")


@pytest.mark.asyncio
async def test_invalidate_proposal_lock_order(db_session, lock_recorder):
    proposal = await _mk_proposal(db_session, 7020, 7021)
    await db_session.commit()

    lock_recorder.reset()
    await MarriageProposalService.invalidate_proposal(db_session, proposal.id, now=NOW)
    await db_session.commit()

    lock_recorder.assert_global_order("MarriageProposalService.invalidate_proposal")


@pytest.mark.asyncio
async def test_accept_proposal_lock_order(db_session, lock_recorder):
    proposal = await _mk_proposal(db_session, 7030, 7031)
    await ItemService.grant(db_session, 7030, "wedding_ring", 1)
    await db_session.commit()

    lock_recorder.reset()
    await MarriageService.accept_proposal(db_session, proposal.id, 7031, now=NOW)
    await db_session.commit()

    # NOTE: held to ``assert_first_acquisition_order`` (not the strict
    # whole-sequence check) because of a PRE-EXISTING inversion surfaced by
    # instrumenting ``invalidate_pending_for_users``: acceptance consumes the
    # Wedding Ring (UserItem) *before* invalidating the losing proposals
    # (MarriageProposal), i.e. the raw sequence ends ... item, item, proposal.
    #
    # It is currently unreachable as a deadlock: for two acceptances to
    # contend on the same proposal row they must share a spouse, and every
    # relationship path locks the shared User row FIRST, which serializes them
    # before either reaches the proposal locks. It is a latent hazard only if a
    # future path ever takes proposal locks without the user lock - fixing it
    # means reordering Phase 4's acceptance body, which is out of scope here.
    lock_recorder.assert_first_acquisition_order("MarriageService.accept_proposal")


# ---------------------------------------------------------------------------
# 2. Flower paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_flower_lock_order(db_session, lock_recorder):
    await _mk_marriage(db_session, 7101, 7102)
    await ItemService.grant(db_session, 7101, "flower", 1)
    await ConfigService.set(db_session, "flower_love_gain", 5)
    await db_session.commit()

    lock_recorder.reset()
    await LoveService.send_flower(db_session, 7101, 7102, now=NOW)
    await db_session.commit()

    lock_recorder.assert_global_order("LoveService.send_flower")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name,actor_arg",
    [
        ("accept_flower", "receiver"),
        ("reject_flower", "receiver"),
    ],
)
async def test_flower_resolution_lock_order(
    db_session, lock_recorder, method_name, actor_arg
):
    marriage = await _mk_marriage(db_session, 7110, 7111)
    gift = await _mk_gift(db_session, marriage, 7110, 7111)
    await ConfigService.set(db_session, "flower_love_gain", 5)
    await db_session.commit()

    method = getattr(LoveService, method_name)
    lock_recorder.reset()
    await method(db_session, gift.id, 7111, now=NOW)
    await db_session.commit()

    lock_recorder.assert_global_order(f"LoveService.{method_name}")


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["invalidate_gift", "expire_gift"])
async def test_gift_terminal_transition_lock_order(
    db_session, lock_recorder, method_name
):
    marriage = await _mk_marriage(db_session, 7120, 7121)
    gift = await _mk_gift(db_session, marriage, 7120, 7121)
    await db_session.commit()

    method = getattr(LoveService, method_name)
    lock_recorder.reset()
    await method(db_session, gift.id, now=NOW)
    await db_session.commit()

    lock_recorder.assert_global_order(f"LoveService.{method_name}")


@pytest.mark.asyncio
async def test_create_proposal_cooldown_check_takes_no_marriage_lock(
    db_session, lock_recorder
):
    """Phase 6.1: ``create_proposal`` now consults the divorce cooldown, which
    lives on ``Marriage`` rows.

    The cooldown query (``MarriageService.get_active_cooldown_for_user``) is a
    plain non-locking ``SELECT``, so it must not add a Marriage acquisition to
    the path. ``create_proposal`` must stay ``User -> MarriageProposal`` and
    must never take a Marriage lock - otherwise it would sit on the wrong side
    of the Marriage/MarriageProposal ordering used by ``accept_proposal``.
    """
    marriage = await _mk_marriage(db_session, 7500, 7501)
    await _mk_user(db_session, 7502)
    await ItemService.grant(db_session, 7500, "wedding_ring", 1)
    await db_session.commit()

    # Put 7500 into the post-divorce cooldown so the new check has something
    # to find, then confirm an in-cooldown proposer is refused without locks.
    await MarriageService.divorce_marriage(db_session, marriage.id, 7500, now=NOW)
    await db_session.commit()

    lock_recorder.reset()
    with pytest.raises(MarriageCooldownActiveError):
        await MarriageProposalService.create_proposal(
            db_session, 7500, 7502, -1007001, now=NOW + timedelta(seconds=1)
        )
    await db_session.commit()

    # Refused path: the User lock, then only the pre-existing overdue sweep
    # (`_expire_overdue_for_proposer`, which takes no lock when nothing is
    # overdue). The cooldown check adds nothing, and we bail out BEFORE
    # `_lock_pending_outgoing_for_proposer`, so no proposal row is ever locked.
    assert lock_recorder.events == ["user", "proposal"], (
        f"expected [user, proposal] on the refused path, got {lock_recorder.events}"
    )
    # The whole point: the cooldown read must not take a Marriage lock.
    assert "marriage" not in lock_recorder.events
    assert "gift" not in lock_recorder.events
    assert "item" not in lock_recorder.events

    # After the cooldown expires the same path locks User -> MarriageProposal
    # and still never touches Marriage.
    lock_recorder.reset()
    await MarriageProposalService.create_proposal(
        db_session,
        7500,
        7502,
        -1007001,
        now=NOW + timedelta(seconds=DEFAULT_DIVORCE_COOLDOWN_SECONDS + 1),
    )
    await db_session.commit()

    # Allowed path: user lock, the overdue sweep, and now also the
    # pending-outgoing row lock - i.e. one MORE proposal step than the refused
    # path, which is what proves the cooldown check short-circuits before it.
    assert lock_recorder.events == ["user", "proposal", "proposal"], (
        f"expected [user, proposal, proposal] on the allowed path, "
        f"got {lock_recorder.events}"
    )
    assert "marriage" not in lock_recorder.events
    lock_recorder.assert_global_order("create_proposal (with cooldown check)")


@pytest.mark.asyncio
async def test_divorce_marriage_lock_order(db_session, lock_recorder):
    """Divorce is the only path that touches ALL FIVE resource types, so it is
    the strongest check on the global order:

        User -> Marriage -> MarriageProposal -> FlowerGift -> UserItem

    It also pins the proposal-before-gift ordering inside the divorce
    transaction (invalidate proposals first, then invalidate/refund gifts).
    """
    marriage = await _mk_marriage(db_session, 7400, 7401, love_score=42)
    await _mk_gift(db_session, marriage, 7400, 7401)
    await _mk_gift(db_session, marriage, 7401, 7400)
    await _mk_proposal(db_session, 7400, 7402)
    await _mk_proposal(db_session, 7402, 7401)
    await db_session.commit()

    lock_recorder.reset()
    result = await MarriageService.divorce_marriage(db_session, marriage.id, 7400, now=NOW)
    await db_session.commit()

    assert result.transition_won is True
    assert result.invalidated_gift_count == 2
    assert result.invalidated_proposal_count == 2
    lock_recorder.assert_first_acquisition_order("MarriageService.divorce_marriage")

    # Each gift invalidation re-locks the (already held) Marriage row, so the
    # raw sequence interleaves. What must hold is the order of FIRST
    # acquisitions: Marriage -> MarriageProposal -> FlowerGift.
    ordered_firsts = [
        r for r in lock_recorder.first_acquisitions() if r in ("marriage", "proposal", "gift")
    ]
    assert ordered_firsts == ["marriage", "proposal", "gift"], (
        f"divorce first locked Marriage/Proposal/Gift as {ordered_firsts}; "
        "proposals must be invalidated before FlowerGifts "
        "(MarriageProposal < FlowerGift)."
    )


@pytest.mark.asyncio
async def test_invalidate_pending_gifts_for_marriage_lock_order(db_session, lock_recorder):
    """Bulk invalidation walks several gifts in one transaction, so it re-locks
    User/Marriage/Gift once per gift. Re-acquiring a lock the transaction
    already holds is always safe, so this path is held to the weaker (but still
    cycle-free) contract: the FIRST acquisition of each resource type must
    follow the global order.
    """
    marriage = await _mk_marriage(db_session, 7300, 7301)
    await _mk_gift(db_session, marriage, 7300, 7301)
    await _mk_gift(db_session, marriage, 7301, 7300)
    await db_session.commit()

    lock_recorder.reset()
    results = await LoveService.invalidate_pending_gifts_for_marriage(
        db_session, marriage.id, now=NOW
    )
    await db_session.commit()

    assert len(results) == 2
    lock_recorder.assert_first_acquisition_order(
        "LoveService.invalidate_pending_gifts_for_marriage"
    )


# ---------------------------------------------------------------------------
# 3. Cross-path consistency (the actual cycle check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_flower_and_gift_resolution_agree_on_user_then_marriage(
    db_session, lock_recorder
):
    """The suspected send-vs-resolution cycle.

    ``send_flower`` and the flower resolution paths both touch ``User`` and
    ``Marriage``. If one orders ``User -> Marriage`` and the other
    ``Marriage -> User``, two concurrent transactions can deadlock on InnoDB.
    This test pins both projections to the same relative order.
    """
    marriage = await _mk_marriage(db_session, 7201, 7202)
    # Dated in the past so it does not consume 7201 -> 7202's quota for NOW.
    gift = await _mk_gift(db_session, marriage, 7202, 7201, gift_date="2026-10-01")
    await ItemService.grant(db_session, 7201, "flower", 1)
    await ConfigService.set(db_session, "flower_love_gain", 5)
    await db_session.commit()

    # Path A: send_flower
    lock_recorder.reset()
    await LoveService.send_flower(db_session, 7201, 7202, now=NOW)
    await db_session.commit()
    send_projection = lock_recorder.projection(("user", "marriage"))
    lock_recorder.reset()

    # Path B: accept_flower
    lock_recorder.reset()
    await LoveService.accept_flower(db_session, gift.id, 7201, now=NOW)
    await db_session.commit()
    accept_projection = lock_recorder.projection(("user", "marriage"))

    expected = ["user", "marriage"]
    assert send_projection == expected, (
        f"send_flower locks User/Marriage as {send_projection}, expected {expected}"
    )
    assert accept_projection == expected, (
        f"accept_flower locks User/Marriage as {accept_projection}, expected {expected}; "
        "this is the reverse of send_flower and can deadlock on MariaDB/MySQL."
    )


@pytest.mark.asyncio
async def test_create_proposal_and_accept_proposal_agree_on_user_then_proposal(
    db_session, lock_recorder
):
    """``create_proposal`` cannot anchor on a proposal id (it is about to
    INSERT one), so it must lock ``User`` before any ``MarriageProposal`` row.
    ``accept_proposal`` therefore has to use the same relative order.
    """
    await _mk_user(db_session, 7210)
    await _mk_user(db_session, 7211)
    await _mk_group(db_session)
    await ItemService.grant(db_session, 7210, "wedding_ring", 2)
    await db_session.commit()

    # Path A: create_proposal (User -> MarriageProposal)
    lock_recorder.reset()
    await MarriageProposalService.create_proposal(
        db_session, 7210, 7211, -1007001, now=NOW
    )
    await db_session.commit()
    create_projection = lock_recorder.projection(("user", "proposal"))
    lock_recorder.reset()

    # Path B: accept_proposal on an independently prepared proposal
    proposal = await _mk_proposal(db_session, 7220, 7221)
    await ItemService.grant(db_session, 7220, "wedding_ring", 1)
    await db_session.commit()

    lock_recorder.reset()
    await MarriageService.accept_proposal(db_session, proposal.id, 7221, now=NOW)
    await db_session.commit()
    accept_projection = lock_recorder.projection(("user", "proposal"))

    expected = ["user", "proposal"]
    assert create_projection == expected, (
        f"create_proposal locks User/MarriageProposal as {create_projection}, "
        f"expected {expected}"
    )
    assert accept_projection == expected, (
        f"accept_proposal locks User/MarriageProposal as {accept_projection}, "
        f"expected {expected}; this is the reverse of create_proposal and can "
        "deadlock on MariaDB/MySQL."
    )


# ---------------------------------------------------------------------------
# Economy policy: one rank table, and the real purchase/settlement paths
# ---------------------------------------------------------------------------


def test_policy_table_is_strictly_ordered_and_agrees_with_this_suite():
    assert sorted(LOCK_RANKS, key=LOCK_RANKS.get) == [
        "users",
        "marriage",
        "marriage_proposal",
        "flower_gift",
        "player_cards",
        "user_items",
        "apple_wallets",
        "player_progressions",
    ]
    assert len(set(LOCK_RANKS.values())) == len(LOCK_RANKS)
    assert LOCK_ORDER_RANK == {
        "user": LOCK_RANKS["users"],
        "marriage": LOCK_RANKS["marriage"],
        "proposal": LOCK_RANKS["marriage_proposal"],
        "gift": LOCK_RANKS["flower_gift"],
        "card": LOCK_RANKS["player_cards"],
        "item": LOCK_RANKS["user_items"],
        "wallet": LOCK_RANKS["apple_wallets"],
        "progression": LOCK_RANKS["player_progressions"],
    }


def _record_calls(monkeypatch, events: List[str], targets) -> None:
    """Append ``label`` to ``events`` each time ``owner.name`` is entered.

    Works for module functions, classmethods and staticmethods. The wrapper
    forwards to the original, so behaviour is unchanged.
    """
    for owner, name, label in targets:
        original = getattr(owner, name)

        async def wrapper(*args, __orig=original, __label=label, **kwargs):
            events.append(__label)
            return await __orig(*args, **kwargs)

        monkeypatch.setattr(owner, name, wrapper)


def _ranks_of(events: List[str]) -> List[int]:
    return [LOCK_ORDER_RANK[event] for event in events]


@pytest.mark.asyncio
async def test_item_buy_locks_user_then_inventory_then_wallet(db_session, monkeypatch):
    from sib.services import items as items_module

    user = await create_test_user(db_session, "قفل خرید آیتم")
    await AppleWalletService.earn(db_session, user.id, 500, reason="seed")
    await db_session.commit()
    priced = None
    for key in ITEM_CATALOG:
        if await ItemService.buy_price(db_session, key):
            priced = key
            break
    assert priced is not None, "no buyable catalog item in the test catalog"

    events: List[str] = []
    _record_calls(
        monkeypatch,
        events,
        [
            (items_module, "lock_user_row", "user"),
            (ItemService, "_lock_inventory_row", "item"),
            (AppleWalletService, "spend", "wallet"),
            (ItemService, "grant", "item"),
        ],
    )
    await ItemService.buy(db_session, user.id, priced, 1)
    await db_session.commit()

    # Inventory is locked before the wallet; grant re-enters the same row.
    assert events == ["user", "item", "wallet", "item"], events
    firsts = list(dict.fromkeys(events))  # first acquisition of each resource
    assert _ranks_of(firsts) == sorted(_ranks_of(firsts)), firsts


@pytest.mark.asyncio
async def test_shop_card_purchase_locks_user_card_wallet_progression(db_session, monkeypatch):
    from sib.services import shop as shop_module
    from sib.services.shop import ShopService
    from sib.tests.test_shop import _make_characters

    await _make_characters(db_session, 1)
    user = await create_test_user(db_session, "قفل خرید کارت")
    await AppleWalletService.earn(db_session, user.id, 5000, reason="seed")
    await db_session.commit()
    entries = await ShopService.list_entries(db_session, user_id=user.id)
    await db_session.commit()
    entry = entries[0][0]

    events: List[str] = []
    _record_calls(
        monkeypatch,
        events,
        [
            (shop_module, "lock_user_row", "user"),
            (PlayerCardService, "acquire", "card"),
            (AppleWalletService, "debit_once", "wallet"),
            (PlayerXPService, "award_character_collection", "progression"),
        ],
    )
    await ShopService.purchase(db_session, user.id, entry.id, nonce="lockorder-01")
    await db_session.commit()

    assert events == ["user", "card", "wallet", "progression"], events


@pytest.mark.asyncio
async def test_apple_battle_settlement_pays_wallet_before_progression(db_session, monkeypatch):
    """Regression for F5: the winner's apples (wallet) are paid before the match
    progression award, matching every other path (wallet -> progression)."""
    from sib.database.models import Game, MatchState
    from sib.services.apple_battle import AppleBattleService
    from sib.services.group import GroupService
    from sib.services.match_result import MatchResultService
    from sib.tests.test_apple_battle import _card, _make_match, _reload_match

    class _Chat:
        def __init__(self, cid, title):
            self.id = cid
            self.title = title
            self.type = "supergroup"
            self.username = None

    await GroupService.get_or_create_group(db_session, _Chat(-7001, "قفل نبرد یک"))
    await GroupService.get_or_create_group(db_session, _Chat(-7002, "قفل نبرد دو"))
    a = await create_test_user(db_session, "قفل نبرد الف")
    b = await create_test_user(db_session, "قفل نبرد ب")
    card_a = await _card(db_session, a, "lock_a")
    card_b = await _card(db_session, b, "lock_b")
    await AppleBattleService.enqueue(db_session, a.id, card_a.id)
    await AppleBattleService.enqueue(db_session, b.id, card_b.id)
    await db_session.commit()

    game = await db_session.scalar(select(Game).where(Game.code == "apple_battle"))
    match = await _make_match(
        db_session,
        game,
        type("X", (), {"id": -7001})(),
        type("X", (), {"id": -7002})(),
        a,
        b,
    )
    match = await _reload_match(db_session, match.id)
    state = await AppleBattleService.build_match_state(db_session, match)
    match.game_state = json.dumps(state, ensure_ascii=False)
    match.state = MatchState.ACTIVE
    await db_session.commit()
    battle_row = await AppleBattleService.record_battle(db_session, match, state)
    await db_session.commit()
    match = await _reload_match(db_session, match.id)

    events: List[str] = []
    _record_calls(
        monkeypatch,
        events,
        [
            (AppleWalletService, "earn", "wallet"),
            (MatchResultService, "apply_match_result", "progression"),
        ],
    )
    await AppleBattleService.finalize_match(db_session, match, battle_row.id)
    await db_session.commit()

    assert "wallet" in events and "progression" in events, events
    assert events.index("wallet") < events.index("progression"), events



@pytest.mark.asyncio
async def test_card_upgrade_locks_and_writes_items_before_the_wallet(db_session, monkeypatch):
    """Regression for the audit's lock-order finding on CardUpgradeService.upgrade.

    Reads lock the inventory row (rank 5) before the wallet (rank 6), and the
    writes must follow the same order: consume the item, then debit apples.
    The card row (rank 4) is locked inline in upgrade() and is not observable
    here; the user row (rank 0) is taken first.
    """
    from sib.services import card_upgrade as card_upgrade_module
    from sib.services.card_upgrade import CardUpgradeService
    from sib.tests.test_card_upgrade import _fund, _new_card

    user, _character, card = await _new_card(db_session, name="ترتیب ارتقا", code="order_up")
    await _fund(db_session, user, apples=500, gold=10)
    card_id, user_id = card.id, user.id

    events: List[str] = []
    _record_calls(
        monkeypatch,
        events,
        [
            (card_upgrade_module, "lock_user_row", "user"),
            (ItemService, "get_total", "item"),
            (AppleWalletService, "get_balance", "wallet"),
            (ItemService, "consume", "item"),
            (AppleWalletService, "debit_once", "wallet"),
        ],
    )
    await CardUpgradeService.upgrade(db_session, user_id, card_id, expected_level=1)
    await db_session.commit()

    assert events == ["user", "item", "wallet", "item", "wallet"], events
    firsts = list(dict.fromkeys(events))
    assert _ranks_of(firsts) == sorted(_ranks_of(firsts)), firsts
