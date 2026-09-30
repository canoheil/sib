"""Persian user-facing error messages and exceptions."""


class sibException(Exception):
    """Base exception for sib bot with friendly Persian message."""
    def __init__(self, message: str = "اوپس! یه مشکلی پیش اومد، یه کم دیگه دوباره تلاش کن 🍏"):
        self.message = message
        super().__init__(self.message)


class MembershipRequiredException(sibException):
    def __init__(self):
        super().__init__(
            "🍏 برای شروع بازی‌ها و استفاده از سیب، اول باید عضو کانال رسمیمون بشی!\n"
            "بعد از عضویت روی دکمه «بررسی عضویت» بزن."
        )


class ActiveSessionExistsException(sibException):
    def __init__(self):
        super().__init__(
            "🍏 سیب حواسش هست! الان یه بازی، صف یا پارتی فعال داری.\n"
            "اول اون رو تموم کن یا انصراف بده، بعد سراغ بعدی بیا!"
        )


class PartyFullException(sibException):
    def __init__(self):
        super().__init__("ظرفیت این تیم تکمیل شده و جای خالی نداره 🍏")


class PartyNotFoundException(sibException):
    def __init__(self):
        super().__init__("این پارتی دیگه فعال نیست یا لغو شده 🍏")


class MatchNotFoundException(sibException):
    def __init__(self):
        super().__init__("مسابقه مورد نظر پیدا نشد یا قبلاً به پایان رسیده 🍏")


class InvalidActionException(sibException):
    def __init__(self, detail: str = "حرکت مجاز نیست!"):
        super().__init__(f"🍏 حرکت نامعتبر: {detail}")


class NotYourTurnException(sibException):
    def __init__(self):
        super().__init__("🍏 هنوز نوبتت نشده، عجله نکن!")


class StaleCallbackException(sibException):
    def __init__(self):
        super().__init__("🍏 این دکمه منقضی شده یا وضعیت بازی تغییر کرده.")


class UnauthorizedAdminException(sibException):
    def __init__(self):
        super().__init__("🍏 برای بخش مدیریت سیب، دسترسی لازم رو نداری!")


# General Persian error messages
MESSAGES = {
    "generic_error": "اوپس! یه خطای غیرمنتظره پیش اومد. سیب داره رسیدگیش می‌کنه 🍏",
    "bot_not_admin_in_channel": "سیب هنوز دسترسی بررسی عضویت در کانال رو نداره. مدیر کانال باید ربات رو ادمین کنه 🍏",
    "membership_check_success": "عالیه! عضویتت در کانال تأیید شد. خوش اومدی به دنیای سیب 🍏✨",
    "membership_check_failed": "هنوز عضو کانال نشدی یا عضویتت تأیید نشده! اول عضو شو، بعد دوباره دکمه رو بزن 🍏",
    "private_chat_welcome": (
        "🍏 **سلام! من سیب‌ام!**\n\n"
        "من یه ربات بازی گروهی‌ام که می‌تونی من رو به گروه‌هاتون اضافه کنی و با گروه‌های دیگه مسابقه بدی!\n\n"
        "🎮 بازی‌های هیجان‌انگیز، سیستم ریتینگ تیمی و کل‌کل بین گروه‌ها!\n\n"
        "برای شروع کافیه من رو به گروه‌تون اضافه کنی و دسترسی ارسال پیام بدی 👇"
    ),
    "group_chat_welcome": (
        "🍏 **سیب وارد گروه شد! سلام به همگی!**\n\n"
        "گروه شما ثبت شد و ریتینگ اولیه ۱۵۰۰ براتون در نظر گرفته شد.\n"
        "برای شروع مسابقات و باز کردن منوی بازی‌ها، دکمه زیر رو لمس کنید 👇"
    ),
    "already_registered": "شما قبلاً در سیب ثبت شدی و حسابت فعاله 🍏",
}
