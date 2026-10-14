"""Pure Persian cinematic text rendering; no Telegram formatting or I/O.

Each event owns five immutable alternatives. The caller supplies real display
names, a seeded RNG, and the already-decided event; no game state is changed.
"""

import random


_ANNOUNCEMENT = (
    "بر فراز {region_name}، سپیده به رنگ خاکستر درآمده است. {username}، {card_name} را به گذرگاه ببر؛ {enemy_name} از مه بیرون آمده و به دروازه‌ها رحم نخواهد کرد.",
    "آب‌های {region_name} بی‌صدا تیره شدند. {username}، در کنار {card_name} بایست؛ {enemy_name} پشت آخرین موج کمین کرده است.",
    "زنگ‌های {region_name} بی‌آنکه دستی به آن‌ها برسد به صدا درآمدند. {username}، {card_name} را فرا بخوان؛ {enemy_name} بر آستانهٔ شهر ایستاده است.",
    "بادِ {region_name} بوی آهن می‌دهد. {username} و {card_name} باید پیش از فروریختن نخستین برج برسند؛ رد پای {enemy_name} روی خاک مانده است.",
    "فانوس‌های {region_name} رو به تاریکی می‌سوزند. {username}، {card_name} را کنار بارو نگه دار: نشانِ {enemy_name} در افق پیدا شده است.",
)

_JOIN = (
    "{username} با {card_name} از میان غبار گذشت؛ ضربه‌ای به اندازهٔ {damage} بر زره شیاسیب نشست. هنوز {remaining} از جان دشمن مانده است.",
    "{card_name} به اشارهٔ {username} پیش آمد و صف دشمن را شکافت. {damage} آسیب بر جای ماند و توان شیاسیب به {remaining} رسید.",
    "در سکوت میدان، {username} راه را برای {card_name} گشود؛ تیغه با نیروی {damage} فرود آمد. شیاسیب با {remaining} جان عقب نشست.",
    "{username} و {card_name} از پشت ستون‌ها بیرون جهیدند؛ سپر دشمن {damage} آسیب دید و هنوز {remaining} توان برایش باقی است.",
    "خاک زیر پای {card_name} لرزید؛ {username} فرمان حمله داد و {damage} آسیب به شیاسیب رسید. جان باقی‌مانده‌اش {remaining} است.",
)

_CRITICAL = (
    "{username} لحظهٔ گشودن زره را دید؛ {card_name} همان‌جا فرود آمد و {damage} آسیب بر جای گذاشت. شیاسیب با {remaining} جان به دشواری ایستاده است.",
    "نورِ {card_name} به فرمان {username} از شکاف تاج دشمن گذشت؛ ضربهٔ سهمگین {damage} از توان شیاسیب کاست و {remaining} برایش ماند.",
    "{username} نفسش را حبس کرد. یک حرکتِ {card_name} با نیروی {damage} صف شیاسیب را از هم درید؛ جان او به {remaining} رسید.",
    "درست هنگام فرود گام دشمن، {username} به {card_name} اشاره کرد؛ زره ترکید، {damage} آسیب ثبت شد و فقط {remaining} جان باقی ماند.",
    "{card_name} از دل سایه بیرون آمد و فرمان {username} را به ضربه‌ای سهمگین بدل کرد؛ {damage} آسیب و {remaining} جانِ باقی‌مانده، بهای آن لحظه بود.",
)

_HIGH_DAMAGE = (
    "{username} و {card_name} چنان به دروازهٔ سپاه کوبیدند که لولاها از دیوار کنده شد؛ {damage} آسیب رسید و شیاسیب با {remaining} جان عقب رفت.",
    "موجی از نیروی {card_name} به فرمان {username} سراسر میدان را پیمود؛ صف دشمن زیر {damage} آسیب فرو ریخت و تنها {remaining} جان ماند.",
    "{username} راه سنگر را گشود و {card_name} آوار را بر سپاه شیاسیب فرود آورد؛ {damage} آسیب به جا ماند، توان دشمن {remaining} شد.",
    "با یک حرکتِ {card_name}، هزار بازتاب به یک فروغ بدل شدند؛ {username} شکست صف دشمن را دید: {damage} آسیب و فقط {remaining} جان.",
    "{username} کنار {card_name} بر خاکِ لرزان ایستاد؛ برخوردی با نیروی {damage} سنگرهای شیاسیب را خاموش کرد و {remaining} جان برایش گذاشت.",
)

_FINISHER = (
    "{username} تیغهٔ {card_name} را برای آخرین بار بالا آورد؛ نشانِ {enemy_name} در خاکِ {region_name} فرو رفت و دیگر دستی برای برداشتنش نماند.",
    "به اشارهٔ {username}، {card_name} واپسین رشتهٔ تاریکی را در {region_name} برید. {enemy_name} خاموش شد و نور دوباره به زمین رسید.",
    "ضربهٔ پایان را {card_name} در کنار {username} فرود آورد؛ پژواکِ {enemy_name} در {region_name} شکست و صدای نفس مدافعان شنیده شد.",
    "{username} با {card_name} از میان آوار گذشت؛ آخرین جرقه بر زره {enemy_name} نشست و {region_name} از سایه بیرون آمد.",
    "تاجِ {enemy_name} زیر ضربهٔ {card_name} فرو افتاد. {username} در {region_name} ایستاد تا چراغ نگهبانان دوباره روشن شود.",
)

_VICTORY = (
    "{region_name} هنوز ایستاده است. {username}، رد تیغهٔ {card_name} بر سنگ دروازه مانده؛ مردم نام مدافعان را آهسته می‌گویند و {enemy_name} دیگر پیش نمی‌رود.",
    "نخستین رهگذر پس از نبرد از {region_name} گذشت. {username} و {card_name} کنار راه ایستاده‌اند؛ آب دیگر تصویر {enemy_name} را بازنمی‌گرداند.",
    "پرچم‌های {region_name} دوباره در آفتاب پیدا شدند. {username}، {card_name} را پایین آورد؛ شهر پیش از فریاد پیروزی نفس راحتی کشید و {enemy_name} گریخت.",
    "سنگرهای {region_name} فرو نریختند. {username} و {card_name} به افق نگاه کردند؛ جای پای {enemy_name} هنوز تازه است، اما این خاک امروز از آنِ او نیست.",
    "فانوس‌های {region_name} یکی‌یکی روشن شدند. {username}، {card_name} را کنار گذرگاه نشاند؛ کاروان‌ها در سکوت از کنار میدانِ رهاشده از {enemy_name} گذشتند.",
)

_DEFEAT = (
    "دروازهٔ {region_name} شکست. {username}، {card_name} را از زیر باران سنگ بیرون کشید؛ {enemy_name} این خاک را گرفت، اما صدای بازماندگان خاموش نشده است.",
    "وقتی {username} و {card_name} به کرانه رسیدند، {region_name} دیگر نور را نمی‌پذیرفت. {enemy_name} بر فراز آن پرچم زده؛ راه بازگشت باید دوباره گشوده شود.",
    "برج‌های {region_name} یک‌به‌یک تار شدند. {username}، {card_name} را پشت آخرین دیوار نگه داشت؛ زمین به دست {enemy_name} افتاد، نه سوگند نگهبانانش.",
    "باد در {region_name} ایستاد و دیوار فرو ریخت. {username} و {card_name} آخرین نفرات را به راه امن بردند؛ {enemy_name} زمین را گرفت، نه امیدشان را.",
    "چراغِ {region_name} خاموش شد. {username}، {card_name} را از گذرگاه شکسته گذراند؛ {enemy_name} اکنون بر ویرانه‌ها نگهبانی می‌دهد.",
)

_EXPIRED = (
    "وقتی خبر به {region_name} رسید، پنجرهٔ نبرد گذشته بود. {username} و {card_name} کنار دروازه ایستادند؛ از ساعت‌های بی‌خبر در برابر {enemy_name} هنوز روایتی قطعی در دست نیست.",
    "پیامِ {username} و {card_name} دیر از مهِ {region_name} گذشت. زمان فراخوان پایان یافته بود و هیچ‌کس سرانجام رویارویی با {enemy_name} را از سکوت نمی‌خواند.",
    "در {region_name}، {username} نامِ {card_name} را بر سنگ سرد نوشت؛ پیغام به‌موقع نرسید و خبری از پایان راهِ {enemy_name} در دست نیست.",
    "{username} و {card_name} فانوسِ {region_name} را تا واپسین لحظه روشن نگه داشتند. با بازگشت صدا، فرصت پاسخ به {enemy_name} گذشته بود؛ روایت هنوز ناتمام است.",
    "{username} تصویرِ {card_name} را کنار گذرگاهِ بی‌خبر گذاشت. {region_name} از خاموشی پیام بیرون آمد، اما ساعت‌های رویارویی با {enemy_name} بازنمی‌گردند.",
)


def _fa(number: int) -> str:
    """Use Persian numerals in user-facing text without mutable tables."""
    return "".join("۰۱۲۳۴۵۶۷۸۹"[ord(char) - 48] if "0" <= char <= "9" else char for char in str(number))


def _render(templates: tuple[str, ...], rng: random.Random, **values: object) -> str:
    return templates[rng.randrange(len(templates))].format(**values)


def render_join(
    *, username: str, card_name: str, damage: int, is_crit: bool,
    enemy_hp_remaining: int, rng: random.Random,
) -> str:
    """Describe an already-decided join/attack; never decide damage here."""
    return _render(
        _CRITICAL if is_crit else _JOIN, rng,
        username=username, card_name=card_name,
        damage=_fa(damage), remaining=_fa(enemy_hp_remaining),
    )


def render_high_damage(
    *, username: str, card_name: str, damage: int,
    enemy_hp_remaining: int, rng: random.Random,
) -> str:
    """Explicit high-damage prose; classification threshold is not specified."""
    return _render(
        _HIGH_DAMAGE, rng, username=username, card_name=card_name,
        damage=_fa(damage), remaining=_fa(enemy_hp_remaining),
    )


def render_announcement(
    *, username: str, card_name: str, region_name: str,
    enemy_name: str, rng: random.Random,
) -> str:
    return _render(_ANNOUNCEMENT, rng, username=username, card_name=card_name,
                   region_name=region_name, enemy_name=enemy_name)


def render_finisher(
    *, username: str, card_name: str, region_name: str,
    enemy_name: str, rng: random.Random,
) -> str:
    return _render(_FINISHER, rng, username=username, card_name=card_name,
                   region_name=region_name, enemy_name=enemy_name)


def render_victory(
    *, username: str, card_name: str, region_name: str,
    enemy_name: str, rng: random.Random,
) -> str:
    return _render(_VICTORY, rng, username=username, card_name=card_name,
                   region_name=region_name, enemy_name=enemy_name)


def render_defeat(
    *, username: str, card_name: str, region_name: str,
    enemy_name: str, rng: random.Random,
) -> str:
    return _render(_DEFEAT, rng, username=username, card_name=card_name,
                   region_name=region_name, enemy_name=enemy_name)


def render_expired(
    *, username: str, card_name: str, region_name: str,
    enemy_name: str, rng: random.Random,
) -> str:
    return _render(_EXPIRED, rng, username=username, card_name=card_name,
                   region_name=region_name, enemy_name=enemy_name)
