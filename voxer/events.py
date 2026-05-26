"""Randomised announcement strings for Twitch channel events.

All strings are written in Ukrainian — the primary language of the stream.
Each function picks a random entry from a private list so the announcements
stay fresh across repeated events of the same type.

The lists use a "cult" running joke where the stream community is humorously
referred to as a sect, with the streamer as its leader.

Usage:
    from .events import follow_message, sub_message, ...
    text = follow_message("some_username")  # → random Ukrainian string
"""

import random

# ── Template lists ────────────────────────────────────────────────────────────
# {username}, {months}, {count}, {raider}, {viewers}, {bits} are filled in by
# the corresponding public function via str.format().

_FOLLOW = [
    "{username} вирішив заблукати до нас! Виходу немає — ласкаво просимо до секти!",
    "О боже! {username} натиснув підписку! Стрімер дуже радий черговій заблудшій душі!",
    "{username}, ти тепер офіційно член нашої секти! Виходу немає, але тут затишно і годують!",
    "Нова жертва! {username} щойно підписався! Ми наставимо тебе на істинний шлях, не переживай!",
    "Увага! {username} потрапив до нас! Стрімер дуже радий вітати ще одну заблудшу душу в нашій секті!",
]

_SUBSCRIBE = [
    "{username} підписався і навіть заплатив! Стрімер плаче від щастя! Гроші прийнято, душа куплена!",
    "Неймовірно! {username} підтримав стрімера підпискою! Тепер ти VIP-адепт секти з золотою зіркою!",
    "{username} — справжній меценат нашої секти! Обіцяємо витратити ці гроші виключно на важливі речі!",
    "О господи, {username} зробив підписку! Стрімер зворушений до сліз і вже малює твій портрет на стіні слави!",
]

_RESUB = [
    "{username} повернувся на {months} місяць поспіль! Не міг жити без нас! Ми так і знали!",
    "Вітаємо {username} з {months} місяцями вірності! Ти вже майже повноправний архонт нашої секти!",
    "{months} місяців! {username} не може зупинитись! Лікар сказав, це вже невиліковно, але ми раді!",
    "{username} знову тут! {months} місяців у нашій секті! Орден нагороджує тебе медаллю 'Найвідданіший адепт'!",
]

_GIFT = [
    "{username} роздає підписки як Санта Клаус! {count} нових душ куплено для секти!",
    "Справжній добродій! {username} подарував {count} підписок! Стрімер вже готує тронну промову!",
    "{username} — офіційний меценат секти! {count} нових адептів тобі щиро вдячні!",
    "Боже мій! {username} купив {count} підписок! Хтось сьогодні потрапить в рай, і це {username}!",
]

# Separate list for anonymous gifters (no {username} placeholder)
_GIFT_ANONYMOUS = [
    "Анонімний Санта подарував {count} підписок! Таємний благодійник нашої секти діє!",
    "Хтось анонімний осчасливив нас {count} підписками! Стрімер в шоці від такої щедрості!",
]

_RAID = [
    "РЕЙД! {raider} приводить до нас {viewers} заблудших душ! Ласкаво просимо в секту, виходу немає!",
    "Нас атакують! {raider} з {viewers} людьми! Але ми раді новим членам, заходьте, сідайте!",
    "О боже, рейд від {raider} з {viewers} глядачами! Стрімер вже готує форму для вступу в секту!",
    "{raider} вирішив поділитися з нами {viewers} людьми! Дякуємо! Всі ви тепер у секті!",
]

_CHEER = [
    "{username} кинув {bits} бітів! Монетки прийнято, стрімер вдячний і мало не плаче!",
    "Ого! {bits} бітів від {username}! Стрімер офіційно тепер твій найкращий друг!",
    "{username} спонсорує нашу секту {bits} бітами! Гроші в справі, адепти вдячні!",
    "Бум! {bits} бітів від {username}! Стрімер отримав справжнє натхнення на весь вечір!",
]

# Separate list for anonymous cheers (no {username} placeholder)
_CHEER_ANONYMOUS = [
    "Анонімний спонсор кинув {bits} бітів! Таємний меценат секти не дрімає!",
    "Хтось анонімний пожертвував {bits} бітів! Стрімер вдячний невидимому герою!",
]


# ── Public API ────────────────────────────────────────────────────────────────

def follow_message(username: str) -> str:
    """Return a random funny follow announcement (Ukrainian)."""
    return random.choice(_FOLLOW).format(username=username)


def sub_message(username: str) -> str:
    """Return a random funny new-subscription announcement (Ukrainian)."""
    return random.choice(_SUBSCRIBE).format(username=username)


def resub_message(username: str, months: int) -> str:
    """Return a random funny resubscription announcement (Ukrainian)."""
    return random.choice(_RESUB).format(username=username, months=months)


def gift_message(username: str | None, count: int) -> str:
    """Return a random funny gift-subscription announcement (Ukrainian).

    Picks from the anonymous list when username is None (anonymous gifter).
    """
    if username is None:
        return random.choice(_GIFT_ANONYMOUS).format(count=count)
    return random.choice(_GIFT).format(username=username, count=count)


def raid_message(raider: str, viewers: int) -> str:
    """Return a random funny raid announcement (Ukrainian)."""
    return random.choice(_RAID).format(raider=raider, viewers=viewers)


def cheer_message(username: str | None, bits: int) -> str:
    """Return a random funny cheer (bits) announcement (Ukrainian).

    Picks from the anonymous list when username is None (anonymous cheerer).
    """
    if username is None:
        return random.choice(_CHEER_ANONYMOUS).format(bits=bits)
    return random.choice(_CHEER).format(username=username, bits=bits)
