"""Phase A5 — the structured help tree (ONE source of truth).

The old guide was a flat ``HELP_TOPICS`` dict with eight stale pages: it
never mentioned Ovarya, the Invasion v2, the Apple Mine, the shop chests
or the text shortcuts, and it only described the legacy mini-games.

It is replaced here by a TREE of :class:`HelpSection` objects:

* the index lists every top-level section, one inline button each;
* every section is its own page with a single «🔙 بازگشت به راهنما» button;
* a section MAY declare ``children`` (depth 2 from the index) when its text
  does not fit into one Telegram page.

Adding a section later is one :class:`HelpSection` in ``HELP_SECTIONS``:
the index text, the keyboard and the navigation are all derived from that
list, so nothing else has to be touched.

Copy rules (project's Persian writing skill):
* Persian script only; every number is a Persian digit (through
  :func:`sib.services.sib_claim.fa_digits`);
* ZWNJ (نیم‌فاصله) in every compound: بازی‌ها، کارت‌ها، می‌شود، تازه‌ای ...
* «هٔ» for the ezafe after a silent ه: سایهٔ شیاسیب، دکمهٔ «...»، حلقهٔ ازدواج;
* Persian punctuation only: ، ؛ ؟ and «گیومه» — never an em dash;
* no bureaucratic filler (می‌باشد، لازم به ذکر است) and no rule-of-three
  triplets; the tone stays the game's own voice: direct, warm, epic where
  the world demands it and practical everywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from sib.core.text import escape_markdown
from sib.services.apple_mine import WORK_COOLDOWN_SECONDS
from sib.services.sib_claim import fa_digits

#: The official support handle. It is the ONLY Latin string in the help
#: copy, and it is injected at render time (so the tests can mask it).
SUPPORT_HANDLE = "@sibgame_official"

#: Values injected into the copy (masked by the Persian-only test).
INJECTED_VALUES: Tuple[str, ...] = (SUPPORT_HANDLE,)

#: The label of the single button every section page carries.
HELP_BACK_LABEL = "🔙 بازگشت به راهنما"

#: Callback prefixes of the new tree.
HELP_INDEX_CALLBACK = "help:index"
HELP_SECTION_CALLBACK_PREFIX = "help:section:"

#: The index heading and its one-line introduction.
HELP_INDEX_TITLE = "📖 راهنمای بازی سیب 🍏"
HELP_INDEX_INTRO = (
    "این راهنما کوتاه و کامل است؛ هر بخش را جدا باز کن. "
    "اگر تازه‌ای، از «شروع سریع» شروع کن."
)


@dataclass(frozen=True)
class HelpSection:
    """One page of the guide.

    ``section_key`` is the stable id used in the callback data
    (``help:section:<section_key>``), ``title_fa`` is the button label and
    the page heading, ``body_fa`` is the page text, and ``children`` are
    the optional sub-pages (depth 2 from the index).
    """

    section_key: str
    title_fa: str
    body_fa: str
    children: Tuple["HelpSection", ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# The data behind the copy (numbers are rendered through fa_digits)
# ---------------------------------------------------------------------------

#: Apple Mine pickaxe reward table (M2-RECON; service default is
#: ``pickaxe_rewards`` in services/config_store.py):
#: (level, (min, max) normal apples, gold %, diamond %, obsidian %).
APPLE_MINE_PICKAXE_REWARDS: Tuple[Tuple[int, Tuple[int, int], int, int, int], ...] = (
    (1, (10, 15), 7, 2, 1),
    (2, (15, 22), 10, 3, 1),
    (3, (21, 30), 13, 4, 2),
    (4, (28, 40), 16, 5, 2),
    (5, (45, 60), 20, 6, 3),
)

#: Apple Mine pickaxe upgrade costs in normal Apples, by target level
#: (``pickaxe_upgrade_costs`` in services/config_store.py).
APPLE_MINE_PICKAXE_COSTS: Tuple[Tuple[int, int], ...] = (
    (2, 300),
    (3, 600),
    (4, 1000),
    (5, 1500),
)

#: Keep the guide in sync with the service's cooldown.
APPLE_MINE_COOLDOWN_MINUTES = WORK_COOLDOWN_SECONDS // 60

#: The five shop chests and their price in Apples (data/shop_chests/*.json).
SHOP_CHESTS: Tuple[Tuple[str, int], ...] = (
    ("صندوق سیبی", 150),
    ("صندوق باغ افسون", 80),
    ("صندوق امپراتوری", 200),
    ("صندوق فرزانگان", 200),
    ("صندوق مهتاب", 200),
)

#: Card progression rules (services/card_progression.py).
CARD_MAX_LEVEL = 10
CARD_LEVEL_STEP = 10
CARD_BASE_BUDGET = 60

#: Invasion v2 combat and reward rules
#: (services/invasion_v2/damage.py + services/invasion_v2/rewards.py).
INVASION_CRIT_CHANCE_PERCENT = 10
INVASION_GOLDEN_APPLES = 3
INVASION_DIAMOND_CHANCE_PERCENT = 10
INVASION_OBSIDIAN_CHANCE_PERCENT = 2


def _mine_table() -> str:
    """The Apple Mine pickaxe reward table, one line per level."""
    return "\n".join(
        f"• سطح {fa_digits(level)}: {fa_digits(apples[0])} تا {fa_digits(apples[1])} "
        f"سیب معمولی؛ شانس سیب طلایی {fa_digits(gold)}٪، الماسی "
        f"{fa_digits(diamond)}٪ و ابسیدینی {fa_digits(obsidian)}٪"
        for level, apples, gold, diamond, obsidian in APPLE_MINE_PICKAXE_REWARDS
    )


def _chest_table() -> str:
    """The shop chest price list, one line per chest."""
    return "\n".join(
        f"• {name}: {fa_digits(price)} سیب" for name, price in SHOP_CHESTS
    )


# ---------------------------------------------------------------------------
# The sections
# ---------------------------------------------------------------------------

_QUICK_START = HelpSection(
    section_key="quick_start",
    title_fa="🍏 شروع سریع",
    body_fa=(
        "سیب یک بازی گروهی در سرزمین اواریا است. سیب‌بازان با کارت‌هایشان "
        "در یورش شیاسیب می‌جنگند، در معدن سیب کار می‌کنند و صندوق‌ها را "
        "می‌شکنند.\n\n"
        "**اولین قدم‌ها**\n"
        "۱. سیب را به گروه خودت اضافه کن؛ بیشتر بخش‌های بازی داخل گروه "
        "اتفاق می‌افتد.\n"
        "۲. یک کارت بخر و کارت اصلی پروفایلت را انتخاب کن؛ بدون کارت اصلی "
        "در یورش ضربه نمی‌زنی.\n"
        "۳. کانال رسمی را دنبال کن تا از یورش‌ها باخبر شوی.\n"
        "۴. در گروه بنویس «سیب» تا سیب روزانه‌ات را بگیری.\n\n"
        "**سیب** ارزش اصلی بازی است: با آن از فروشگاه کارت و صندوق می‌خری "
        "و کارت‌هایت را ارتقا می‌دهی."
    ),
)

_WORLD = HelpSection(
    section_key="world",
    title_fa="🌍 دنیای اواریا",
    body_fa=(
        "اواریا سرزمینی است که سایهٔ شیاسیب سال‌ها روی آن افتاده است؛ "
        "باغ‌ها خشک شدند و راه کاروان‌ها بسته شد. حالا شیاسیب با پیاده، "
        "سایه، سردار، سنگین و پادشاه به سوی شهرها می‌آید.\n\n"
        "هر یورش روی یکی از منطقه‌های اواریا باز می‌شود. اگر مدافعان کم "
        "بیایند، آن منطقه به شیاسیب می‌افتد و پرچم سیاه بالا می‌رود. اگر "
        "توان شیاسیب را تا آخر بشکنی، منطقه آزاد می‌شود.\n\n"
        "تو با کارت اصلی‌ات به صف دفاع می‌پیوندی و هر ضربهٔ تو از جان "
        "شیاسیب کم می‌کند. نام تو و نام کارتت در روایت نبرد ثبت می‌شود و "
        "این سرزمین با ضربه‌های تو پیش می‌رود."
    ),
)

_CARDS = HelpSection(
    section_key="cards",
    title_fa="🃏 کارت‌ها",
    body_fa=(
        "هر کارت سه ویژگی دارد: 💪 قدرت، ⚡ سرعت و 🧠 هوش. مجموع ویژگی‌های "
        f"پایهٔ هر شخصیت {fa_digits(CARD_BASE_BUDGET)} است و هر سطح، به هر "
        f"سه ویژگی {fa_digits(CARD_LEVEL_STEP)} واحد اضافه می‌کند.\n\n"
        f"**سطح {fa_digits(1)} تا {fa_digits(CARD_MAX_LEVEL)}:** ارتقا یکی‌یکی "
        "انجام می‌شود و هزینه‌اش سیب معمولی به‌علاوهٔ سیب ویژه است: طلایی، "
        "بعد الماسی و در پایان ابسیدینی.\n\n"
        "**کارت اصلی:** تنها یک کارت می‌تواند کارت اصلی باشد. همین کارت در "
        "یورش شیاسیب و نبرد سیبی به کار می‌رود.\n\n"
        "**کجا کارت بگیرم؟** فروشگاه، صندوق‌ها و صندوقچهٔ گنج روزانه. با "
        "«سیب کارت» کارت‌های خودت را می‌بینی.\n\n"
        "**پوسته:** با «سیب اسکین» ظاهر پروفایلت را بین سیب سبز، سرخ، "
        "طلایی و مهتاب عوض می‌کنی. پوسته فقط ظاهر است و روی قدرت و سطح "
        "اثری ندارد."
    ),
)

_INVASION = HelpSection(
    section_key="invasion",
    title_fa="⚔️ تهاجم شیاسیب",
    body_fa=(
        "شیاسیب هر چند ساعت به یکی از منطقه‌های اواریا یورش می‌آورد. رباتم "
        "یورش را در گروه اعلام می‌کند و دکمهٔ «⚔️ پیوستن به یورش» را زیر "
        "همان پیام می‌گذارد.\n\n"
        "**چطور بجنگم؟** روی همان دکمه بزن؛ کارت اصلی‌ات یک ضربه می‌زند و "
        "جان شیاسیب کم می‌شود. هر نفر در هر یورش فقط یک ضربه می‌زند.\n\n"
        "**قدرت ضربه:** نیروی کارت اصلی با ضریبی میان ۰٫۸۵ تا ۱٫۱۵ ضرب "
        f"می‌شود و {fa_digits(INVASION_CRIT_CHANCE_PERCENT)}٪ شانس ضربهٔ "
        "بحرانی داری؛ آن ضربه آسیب را دو برابر می‌کند. جان شیاسیب از "
        "میانگین قدرت کارت‌های گروه، ضربدر ضریب نوع دشمن و سختی یورش به "
        "دست می‌آید.\n\n"
        "**پاداش:** اگر یورش را ببری، هر مدافع "
        f"{fa_digits(INVASION_GOLDEN_APPLES)} سیب طلایی می‌گیرد؛ با "
        f"{fa_digits(INVASION_DIAMOND_CHANCE_PERCENT)}٪ شانس یک سیب الماسی و "
        f"با {fa_digits(INVASION_OBSIDIAN_CHANCE_PERCENT)}٪ شانس یک سیب "
        "ابسیدینی هم اضافه می‌شود.\n\n"
        "بدون کارت اصلی وارد یورش نمی‌شوی، پس اول کارت اصلی را انتخاب کن."
    ),
)

_MINE = HelpSection(
    section_key="mine",
    title_fa="⛏️ معدن سیبی",
    body_fa=(
        "با «معدن سیبی» معدن را باز می‌کنی و با دکمه «کار کردن در معدن» "
        "یک شیفت کار می‌کنی. هر شیفت فقط یک جایزه می‌دهد و بین دو شیفت "
        f"{fa_digits(APPLE_MINE_COOLDOWN_MINUTES)} دقیقه فاصله لازم است.\n\n"
        f"🔨 کلنگ تو {fa_digits(5)} سطح دارد؛ هرچه سطحش بالاتر برود، سیب "
        "هر شیفت بیشتر می‌شود و شانس سیب‌های ویژه هم بهتر. ارتقا فقط با "
        "سیب معمولی هزینه دارد.\n\n"
        "**جدول جایزه‌ها بر حسب سطح کلنگ**\n"
        f"{_mine_table()}\n\n"
        "**هزینه ارتقا**\n"
        + "\n".join(
            f"• سطح {fa_digits(level)}: {fa_digits(cost)} سیب"
            for level, cost in APPLE_MINE_PICKAXE_COSTS
        )
        + "\n\n"
        "سیب‌های ویژه با تعداد ثابت می‌آیند: ۳ سیب طلایی، ۱ سیب الماسی و "
        "۱ سیب ابسیدینی."
    ),
)

_APPLES = HelpSection(
    section_key="apples",
    title_fa="🎁 سیب‌ها و صندوق‌ها",
    body_fa=(
        "**چهار نوع سیب**\n"
        "🍎 سیب معمولی: ارزش اصلی بازی. با آن از فروشگاه کارت و صندوق "
        "می‌خری و کارت‌هایت را ارتقا می‌دهی.\n"
        "🌕 سیب طلایی: مادهٔ ارتقای کارت در سطح‌های پایین.\n"
        "💎 سیب الماسی: مادهٔ ارتقای کارت در سطح‌های میانی.\n"
        "🌑 سیب ابسیدینی: کمیاب‌ترین سیب و مادهٔ ارتقای کارت در "
        "بالاترین سطح‌ها.\n\n"
        "**پنج صندوق فروشگاه**\n"
        f"{_chest_table()}\n\n"
        "هر صندوق یک جایزه می‌دهد: سیب، یکی از سیب‌های ویژه، پودر جادویی، "
        "گل، حلقهٔ ازدواج یا یک کارت تازه. اگر کارتی را از قبل داشته باشی، "
        "به‌جای کارت سیب می‌گیری.\n\n"
        "**صندوقچهٔ گنج روزانه** («سیب صندوق»): هر روز یک بار در گروه باز "
        "می‌شود و جایزه‌ای تصادفی از سیب معمولی، نهال، سیب طلایی، الماسی، "
        "ابسیدینی یا کارت شخصیت می‌دهد."
    ),
)

_SOCIAL = HelpSection(
    section_key="social",
    title_fa="🌱 باغ، ازدواج، پارتی و مبادله",
    body_fa=(
        "**🌱 باغ سیب:** نهال می‌کاری، صبر می‌کنی تا درخت رشد کند و میوه را "
        "برداشت می‌کنی. شانس هر برداشت، سیب معمولی و سیب‌های ویژه است. با "
        "پودر جادویی درخت را جادویی می‌کنی و شانس سیب‌های ویژه بالا می‌رود. "
        "باغ را دیگران می‌بینند، ولی فقط خودت آن را اداره می‌کنی.\n\n"
        "**💍 ازدواج:** با «ازدواج سیبی» در گروه به کسی پیشنهاد می‌دهی. "
        "برای پیشنهاد، حلقهٔ ازدواج لازم است و حلقه فقط هنگام پذیرش مصرف "
        "می‌شود. با «گل سیبی» به همسرت گل می‌دهی و عشق بالا می‌رود. برای "
        "پایان دادن به ازدواج، «طلاق سیبی» را در پاسخ به پیام همسرت "
        "بنویس.\n\n"
        "**🍏 دوستی سیبی:** روی پیام بازیکن ریپلای کن و «دوستی سیبی» را بفرست؛ "
        "پس از پذیرش، شمار دوستان نامحدود است. با ریپلای «قهر سیبی» فوری "
        "قطع کن؛ فهرست در پروفایل است.\n\n"
        "**🎉 پارتی:** پارتی می‌سازی و دوستانت را دعوت می‌کنی تا با هم "
        "بازی کنید. حالت‌های انفرادی، ۲ به ۲ و ۴ به ۴ وجود دارد. «بازی با "
        "اعضای گروه» هم یک مسابقهٔ دوستانه درون گروه است.\n\n"
        "**🔄 مبادله:** کارت خودت را با کارت دوستت عوض می‌کنی. هر دو باید "
        "کارت هم‌سطح داشته باشید و هر نفر روزی یک بار می‌تواند مبادله کند."
    ),
)

_GAMES = HelpSection(
    section_key="games",
    title_fa="🎮 بازی‌های جانبی",
    body_fa=(
        "بازی را از بخش «🎮 بازی‌ها» در منوی گروه شروع می‌کنی.\n\n"
        "**⚔️ نبرد سیبی:** کارت اصلی‌ات را در نبرد با کارت بازیکن دیگر "
        "می‌سنجی و برنده، نهال جایزه می‌گیرد.\n\n"
        "پنج بازی کلاسیک سیب:\n"
        "• **کارت بالا:** هر نفر یک کارت می‌کشد؛ هرکس کارت بالاتری داشته "
        "باشد می‌برد.\n"
        "• **سنگ، کاغذ، قیچی:** انتخاب پنهان، دورهای پیاپی و یک قهرمان در "
        "پایان.\n"
        "• **دوز:** جدول ۳ در ۳؛ سه نشانه را در یک ردیف جور کن.\n"
        "• **چهار در یک ردیف:** چهار مهره را افقی، عمودی یا مورب کنار هم "
        "بگذار. انفرادی، ۲ به ۲ و ۴ به ۴.\n"
        "• **کشتی جنگی:** ناوگان دشمن را در مه پیدا کن؛ آبی یعنی شلیک به آب "
        "و 💥 یعنی اصابت."
    ),
)

_SHORTCUTS = HelpSection(
    section_key="shortcuts",
    title_fa="⌨️ میان‌برهای عمومی",
    body_fa=(
        "این عبارت‌ها را تنها در چت بنویس تا همان صفحه یا کار انجام شود:\n\n"
        "• **سیب منو**: منوی اصلی\n"
        "• **سیب بازی**: منوی بازی‌های گروه\n"
        "• **سیب** (در گروه): گرفتن سیب روزانه\n"
        "• **سیبام**: دیدن موجودی سیب\n"
        "• **سیب پروفایل** یا **سیب من**: پروفایل خودت\n"
        "• **«پروفایلش»** (با ریپلای روی پیام یک بازیکن): برای دیدن پروفایل آن بازیکن\n"
        "• **لیست دوستان** یا **دوستام**: فهرست دوستان سیبی\n\n"
        "با ریپلای روی پیام مخاطب، «دوستی سیبی» یا «رفاقت سیبی» را برای "
        "درخواست دوستی و «قهر سیبی» را برای قطع دوستی بفرست.\n\n"
        "با ریپلای در گروه می‌توانی «ازدواج سیبی»، «طلاق سیبی»، «گل سیبی»، "
        "«بغل سیبی» یا «بوس سیبی» را هم بفرستی.\n\n"
        "فقط خودِ عبارت را بنویس؛ جمله یا دستور دیگری به آن اضافه نکن."
    ),
)

_SHORTCUT_SECTIONS = HelpSection(
    section_key="shortcut_sections",
    title_fa="⌨️ میان‌برهای بخش‌ها",
    body_fa=(
        "این عبارت‌ها را تنها در چت بفرست تا صفحهٔ همان بخش باز شود:\n\n"
        "• **ویکی سیبی** یا **سیب ویکی**: ویکی شخصیت‌ها\n"
        "• **سیب باغ**، **باغ من** یا **باغ سیبی**: باغ خودت\n"
        "• **قلعه سیبی**: قلعهٔ گروه\n"
        "• **پارتی سیبی**: منوی بازی‌های گروه\n"
        "• **سیب کارت** یا **کارت سیبی**: کارت‌های من\n"
        "• **سیب شاپ** یا **شاپ سیبی**: فروشگاه\n"
        "• **سیب لیدربرد**، **سیب لیدربورد** یا **لیدربرد سیبی**: جدول رتبه‌ها\n"
        "• **سیب اسکین**: تغییر پوستهٔ پروفایل\n"
        "• **سیب راهنما**: همین راهنما\n"
        "• **سیب صندوق** یا **صندوق سیبی**: صندوقچهٔ گنج روزانه\n"
        "• **معدن سیبی**: معدن\n\n"
        "«قلعه سیبی» و «پارتی سیبی» فقط در گروه کار می‌کنند. فقط خودِ عبارت "
        "را بفرست؛ جمله یا دستور دیگری به آن اضافه نکن."
    ),
)

_SUPPORT = HelpSection(
    section_key="support",
    title_fa="📞 پشتیبانی",
    body_fa=(
        "اگر چیزی درست کار نکرد، اول این‌ها را امتحان کن: صفحه را تازه کن، "
        "عضویتت در کانال رسمی را بررسی کن و از مدیر گروه بخواه دسترسی ربات "
        "را کامل کند.\n\n"
        f"**پشتیبانی:** {SUPPORT_HANDLE}\n\n"
        "پیش از پیام دادن، نام گروه و متن خطا را آماده داشته باش تا سریع‌تر "
        "راهنمایی‌ات کنیم."
    ),
)


#: The whole tree. The order here IS the order of the index.
HELP_SECTIONS: Tuple[HelpSection, ...] = (
    _QUICK_START,
    _WORLD,
    _CARDS,
    _INVASION,
    _MINE,
    _APPLES,
    _SOCIAL,
    _GAMES,
    _SHORTCUTS,
    _SHORTCUT_SECTIONS,
    _SUPPORT,
)

#: section_key -> section (top level).
HELP_SECTION_BY_KEY: Dict[str, HelpSection] = {
    section.section_key: section for section in HELP_SECTIONS
}

#: Legacy «help_page:<topic>» callbacks of the old guide -> the new section
#: that replaces them. Old pinned messages keep working.
LEGACY_TOPIC_SECTIONS: Dict[str, str] = {
    "rps": "games",
    "c4": "games",
    "ttt": "games",
    "battleship": "games",
    "high_card": "games",
    "apple_battle": "cards",
    "cards": "cards",
    "garden": "social",
}


def get_section(section_key: Optional[str]) -> Optional[HelpSection]:
    """The top-level section with this key, or ``None``."""
    if not section_key:
        return None
    return HELP_SECTION_BY_KEY.get(section_key)


def get_child(parent: HelpSection, child_key: Optional[str]) -> Optional[HelpSection]:
    """The child of ``parent`` with this key, or ``None``."""
    if not child_key:
        return None
    for child in parent.children:
        if child.section_key == child_key:
            return child
    return None


def render_index_text() -> str:
    """The index page: heading, one line of introduction, the section list."""
    lines = [HELP_INDEX_TITLE, "", HELP_INDEX_INTRO, ""]
    lines.extend(f"• {section.title_fa}" for section in HELP_SECTIONS)
    return "\n".join(lines)


def markdown_safe(text: str) -> str:
    """Escape the INJECTED values for Telegram's legacy Markdown.

    The support handle contains an underscore, and Telegram's legacy
    Markdown parser treats an unpaired ``_`` as a reserved character: the
    whole page would fail with «can't parse entities». The copy itself
    keeps the raw handle (it is the value a player copies); only the
    rendered page escapes it.
    """
    for value in INJECTED_VALUES:
        if value in text:
            text = text.replace(value, escape_markdown(value))
    return text


def render_section_text(section: HelpSection) -> str:
    """One section page: the heading and the body."""
    return f"{section.title_fa}\n\n{markdown_safe(section.body_fa)}"


def section_callback_data(section_key: str, origin: Optional[str] = None) -> str:
    """``help:section:<key>`` (plus the contextual origin when there is one)."""
    from sib.services.nav import with_origin

    return with_origin(f"{HELP_SECTION_CALLBACK_PREFIX}{section_key}", origin)


def index_callback_data(origin: Optional[str] = None) -> str:
    """``help:index`` (plus the contextual origin when there is one)."""
    from sib.services.nav import with_origin

    return with_origin(HELP_INDEX_CALLBACK, origin)


def parse_help_callback(data: Optional[str]) -> Tuple[str, Optional[str]]:
    """Split a help callback into ``(target, origin)``.

    ``target`` is one of:

    * ``index`` — the index page;
    * ``section:<key>`` — a top-level section;
    * ``section:<key>:<child>`` — a sub-page (depth 2 from the index).

    The trailing segment is the contextual origin only when it is a KNOWN
    screen callback (the same allow-list every other screen uses).
    """
    from sib.services.nav import KNOWN_SCREENS

    parts = (data or "").split(":")
    if not parts or parts[0] != "help":
        return "", None
    rest = parts[1:]
    origin: Optional[str] = None
    if len(rest) > 1 and rest[-1] in KNOWN_SCREENS:
        origin = rest[-1]
        rest = rest[:-1]
    if not rest:
        return "", origin
    if rest[0] == "index" and len(rest) == 1:
        return "index", origin
    if rest[0] == "section" and len(rest) in (2, 3):
        return ":".join(rest), origin
    return "", origin
