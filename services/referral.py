"""Friend referral system (Phase REF1, softened to REF2).

How a referral flows:

1. Every player gets a personal Telegram deep link
   ``https://t.me/<bot_username>?start=ref_<user_id>`` (profile ->
   «🎁 دعوت دوستان»).
2. When a *brand-new* user opens that link, :func:`parse_referrer_id`
   extracts the inviter id from the ``/start`` payload and
   ``UserService.get_or_create_user`` stores it in ``users.referred_by_id``
   in the same transaction that creates the account.
3. REF2 (early launch): the reward is paid **immediately** - the invitee
   only has to press ``/start`` in the private chat, no group activity is
   needed any more. :meth:`ReferralService.grant_reward_if_due` runs from
   ``sib.handlers.start`` right after the account is created, so the
   inviter receives 100 apples and the invitee 50 apples, both through the
   auditable ``AppleWalletService`` ledger with per-invitee idempotency
   keys.

Anti-abuse rules (all enforced in this module / ``UserService``):

- No self-referral: the inviter id must differ from the new user's id.
- The inviter must already be a registered, non-bot, non-banned user;
  invented ids are ignored (they would also violate the foreign key).
- Only brand-new accounts can be attributed. An already-registered user
  who opens a referral link keeps ``referred_by_id`` untouched, so an
  existing account can never be (re-)attributed to farm rewards.
- Bots never participate on either side.
- Exactly once: ``users.referral_rewarded`` is claimed with a guarded
  ``UPDATE ... WHERE referral_rewarded = false`` inside the paying
  transaction, and both ledger credits carry idempotency keys. Pressing
  ``/start`` again (private or in a group) therefore never pays twice.

REF2 accepts a higher fake-account risk in exchange for growth: the
group-activity gate is gone. One-time attribution and exactly-once
payment are kept, so the cheapest farming strategy (pressing ``/start``
twice) still pays exactly once per invitee.
"""

from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import logger
from sib.database.models import User
from sib.services.apple_wallet import AppleWalletService
from sib.services.lock_order import lock_user_rows_in_order

REFERRAL_PAYLOAD_PREFIX = "ref_"

INVITER_REWARD_APPLES = 100
INVITEE_REWARD_APPLES = 50

_INVITER_REASON = "referral_inviter"
_INVITEE_REASON = "referral_invitee"

#: Sent to the inviter (private chat) once their reward is *committed*.
INVITER_NOTIFICATION_TEXT = (
    "🎉 یکی از دوستان شما با لینک شما وارد بازی شد! ۱۰۰ سیب هدیه گرفتید."
)

#: Shown to the INVITEE in their /start welcome, right after the 50-apple
#: referral bonus landed in their wallet.
INVITEE_BONUS_TEXT = (
    "🎁 ۵۰ سیب هدیه خوش‌آمدگویی! بابت اومدن با لینک دعوت دوستت، "
    "۵۰ سیب به حسابت اضافه شد."
)


@dataclass(frozen=True)
class ReferralReward:
    """A referral reward that was just paid by ``grant_reward_if_due``."""

    inviter_id: int
    invitee_id: int
    inviter_amount: int = INVITER_REWARD_APPLES
    invitee_amount: int = INVITEE_REWARD_APPLES


def _clean_bot_username(bot_username: str) -> str:
    return (bot_username or "").strip().lstrip("@")


class ReferralService:
    """Deep-link parsing, referrer validation and the one-time reward."""

    # -- deep link --------------------------------------------------------

    @staticmethod
    def parse_referrer_id(args: Optional[str]) -> Optional[int]:
        """Inviter id from a ``/start`` payload, or ``None``.

        Only the exact ``ref_<positive decimal id>`` shape is accepted
        (Telegram deep-link payloads are ``[A-Za-z0-9_-]{1,64}`` anyway);
        anything else - ``ref_``, ``ref_abc``, ``ref_-5``, ``ref_0``, a
        payload meant for another feature - means "no referral".
        """
        if not isinstance(args, str):
            return None
        payload = args.strip()
        if not payload.startswith(REFERRAL_PAYLOAD_PREFIX):
            return None
        digits = payload[len(REFERRAL_PAYLOAD_PREFIX):]
        if not digits or len(digits) > 20 or not digits.isascii() or not digits.isdigit():
            return None
        referrer_id = int(digits)
        return referrer_id if referrer_id > 0 else None

    @staticmethod
    def build_referral_link(bot_username: str, user_id: int) -> str:
        """The personal deep link a player shares with friends."""
        return (
            f"https://t.me/{_clean_bot_username(bot_username)}"
            f"?start={REFERRAL_PAYLOAD_PREFIX}{int(user_id)}"
        )

    @staticmethod
    def build_share_url(bot_username: str, user_id: int) -> str:
        """Telegram's share sheet for the personal deep link."""
        link = ReferralService.build_referral_link(bot_username, user_id)
        text = "🍏 بیا سیب بازی کنیم! با لینک من وارد شو و ۵۰ سیب هدیه بگیر:"
        return (
            f"https://t.me/share/url?url={quote(link, safe='')}"
            f"&text={quote(text, safe='')}"
        )

    # -- attribution -------------------------------------------------------

    @staticmethod
    async def resolve_referrer_id(
        session: AsyncSession,
        *,
        new_user_id: int,
        candidate_referrer_id: Optional[int],
    ) -> Optional[int]:
        """Validate a parsed inviter id for a brand-new account.

        Returns the id to store in ``referred_by_id``, or ``None`` when the
        candidate is unusable: missing, malformed (bools included - they
        are ``int`` subclasses), the new user themselves (self-referral),
        unknown to the game, a bot, or banned.
        """
        candidate = candidate_referrer_id
        if (
            not isinstance(candidate, int)
            or isinstance(candidate, bool)
            or candidate <= 0
            or candidate == new_user_id
        ):
            return None
        referrer = await session.scalar(
            select(User).where(User.id == candidate)
        )
        if referrer is None or referrer.is_bot or referrer.is_banned:
            return None
        return int(referrer.id)

    # -- reward -------------------------------------------------------------

    @staticmethod
    async def has_pending_reward(
        session: AsyncSession, invitee_id: int
    ) -> Optional[bool]:
        """Is a referral reward still awaiting payment for this user?

        ``None`` means the user is not registered yet (callers should try
        again later); ``False`` is final (no inviter, or already paid).
        Since REF2 the window is tiny: the reward is paid in the very same
        transaction that records the attribution.
        """
        row = (
            await session.execute(
                select(User.referred_by_id, User.referral_rewarded).where(
                    User.id == invitee_id
                )
            )
        ).first()
        if row is None:
            return None
        referred_by_id, rewarded = row
        return referred_by_id is not None and not rewarded

    @classmethod
    async def grant_reward_if_due(
        cls, session: AsyncSession, invitee_id: int
    ) -> Optional[ReferralReward]:
        """Pay the referral reward for ``invitee_id`` if it is due.

        Call this right after the invitee registered through a
        ``?start=ref_<inviter id>`` deep link (REF2: the invitee only has
        to press ``/start`` - no group activity required). Returns the
        paid :class:`ReferralReward`, or ``None`` when there is nothing to
        pay (unknown user, no inviter, already paid, invalid inviter).
        The caller owns the transaction: on rollback the claim and both
        credits roll back together, so a failed payment can be retried.
        Notify the inviter only *after* commit, via :meth:`notify_inviter`.

        Strictly idempotent: the one-time claim is a guarded
        ``UPDATE ... WHERE referral_rewarded = false`` under the user row
        lock, and both ledger credits carry per-invitee idempotency keys.
        Calling it again for the same invitee pays nothing.
        """
        if (
            not isinstance(invitee_id, int)
            or isinstance(invitee_id, bool)
            or invitee_id <= 0
        ):
            return None

        # Unlocked probe to learn the inviter id (and to keep the common
        # "no referral" path to a single indexed read).
        invitee = await session.scalar(
            select(User).where(User.id == invitee_id)
        )
        if (
            invitee is None
            or invitee.referred_by_id is None
            or invitee.referral_rewarded
        ):
            return None
        inviter_id = int(invitee.referred_by_id)
        if inviter_id == invitee_id:  # corrupt/manual row; never pay it
            return None

        # Economy lock policy (services/lock_order.py): user rows first
        # (rank 0), ascending id, wallets afterwards (rank 6).
        try:
            await lock_user_rows_in_order(session, [invitee_id, inviter_id])
        except ValueError:
            # The inviter row no longer exists; nothing can be paid.
            return None

        # Re-read under the lock: a concurrent grant may have won while we
        # waited - the locking read sees its committed state.
        invitee = await session.scalar(
            select(User)
            .where(User.id == invitee_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            invitee is None
            or invitee.referred_by_id is None
            or int(invitee.referred_by_id) != inviter_id
            or invitee.referral_rewarded
        ):
            return None
        inviter = await session.scalar(
            select(User)
            .where(User.id == inviter_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if inviter is None or inviter.is_bot:
            return None

        # Claim the one-time reward atomically. Even without real row
        # locks (SQLite tests) the guarded UPDATE + unique ledger keys
        # below keep the payment exactly-once.
        result = await session.execute(
            update(User)
            .where(
                User.id == invitee_id,
                User.referred_by_id == inviter_id,
                User.referral_rewarded.is_(False),
            )
            .values(referral_rewarded=True)
        )
        if result.rowcount != 1:
            return None

        await AppleWalletService.earn(
            session,
            inviter_id,
            INVITER_REWARD_APPLES,
            reason=_INVITER_REASON,
            ref_type="referral",
            ref_id=invitee_id,
            idempotency_key=f"{_INVITER_REASON}:{invitee_id}",
        )
        await AppleWalletService.earn(
            session,
            invitee_id,
            INVITEE_REWARD_APPLES,
            reason=_INVITEE_REASON,
            ref_type="referral",
            ref_id=invitee_id,
            idempotency_key=f"{_INVITEE_REASON}:{invitee_id}",
        )
        logger.info(
            "Referral reward paid: inviter=%s invitee=%s", inviter_id, invitee_id
        )
        return ReferralReward(inviter_id=inviter_id, invitee_id=invitee_id)

    # -- leaderboard (the referral contest board) ---------------------------

    @staticmethod
    async def count_referrals(session: AsyncSession, inviter_id: int) -> int:
        """How many players this inviter brought in (paid referrals).

        REF2 pays the reward in the very same transaction that records
        the attribution (``users.referred_by_id`` + ``referral_rewarded``),
        so every counted row is a referral that really paid out - the
        contest board can count on this number.
        """
        if (
            not isinstance(inviter_id, int)
            or isinstance(inviter_id, bool)
            or inviter_id <= 0
        ):
            return 0
        counted = await session.scalar(
            select(func.count(User.id)).where(User.referred_by_id == inviter_id)
        )
        return int(counted or 0)

    @staticmethod
    async def leaderboard(
        session: AsyncSession, limit: int = 20
    ) -> list[tuple[int, int]]:
        """``(inviter_id, referral_count)`` rows - the referral contest board.

        Counts every referral attributed to an inviter (see
        :meth:`count_referrals`), most referrals first. Ties are broken by
        the smaller inviter id, so the order is stable between runs.
        """
        safe_limit = max(1, int(limit))
        rows = await session.execute(
            select(User.referred_by_id, func.count(User.id).label("referrals"))
            .where(User.referred_by_id.is_not(None))
            .group_by(User.referred_by_id)
            .order_by(func.count(User.id).desc(), User.referred_by_id.asc())
            .limit(safe_limit)
        )
        return [(int(row[0]), int(row[1])) for row in rows.all()]

    # -- notification -------------------------------------------------------

    @staticmethod
    async def notify_inviter(bot, reward: Optional[ReferralReward]) -> bool:
        """Best-effort private message to the inviter after commit.

        Never raises: a blocked bot or a missing private chat must not
        affect the already-paid reward or the invitee's own update.
        """
        if bot is None or reward is None:
            return False
        try:
            await bot.send_message(
                chat_id=reward.inviter_id,
                text=INVITER_NOTIFICATION_TEXT,
                parse_mode=None,
            )
            return True
        except Exception:
            logger.debug(
                "Referral inviter notification failed: inviter=%s",
                reward.inviter_id,
                exc_info=True,
            )
            return False
