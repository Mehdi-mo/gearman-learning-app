"""
Freemium access rules.

Every view asks this module instead of re-implementing the limits, so there is
exactly one definition of what a free account may do.
"""
from django.db.models import F
from django.utils import timezone

from .models import ContentUnlock, DailyUsage

# Free tier allowances.
FREE_UNLOCKS_PER_DAY      = 1     # articles + conversations share this budget
FREE_TRANSLATIONS_PER_DAY = 100   # AI translation calls


def has_premium(user):
    if not user.is_authenticated:
        return False
    try:
        return user.profile.has_premium
    except Exception:
        return False


# --------------------------------------------------------------------------
# Content unlocks
# --------------------------------------------------------------------------

def _target_filter(article=None, conversation=None):
    if article is not None:
        return {'article': article, 'conversation': None}
    return {'article': None, 'conversation': conversation}


def is_unlocked(user, article=None, conversation=None):
    """Already unlocked at some point — permanent, regardless of date."""
    return ContentUnlock.objects.filter(
        user=user, **_target_filter(article, conversation)
    ).exists()


def unlocks_used_today(user):
    return ContentUnlock.objects.filter(
        user=user, unlocked_on=timezone.localdate()
    ).count()


def unlocks_left_today(user):
    """Remaining unlocks, or None for unlimited."""
    if has_premium(user):
        return None
    return max(0, FREE_UNLOCKS_PER_DAY - unlocks_used_today(user))


def try_unlock(user, article=None, conversation=None):
    """
    Decide whether `user` may open this content, recording the unlock if so.

    Returns True when access is granted. Premium accounts and anything already
    unlocked always pass without consuming quota.
    """
    if has_premium(user):
        return True
    if is_unlocked(user, article, conversation):
        return True
    if unlocks_used_today(user) >= FREE_UNLOCKS_PER_DAY:
        return False

    # get_or_create keeps a double-click from burning two unlocks.
    ContentUnlock.objects.get_or_create(
        user=user,
        defaults={'unlocked_on': timezone.localdate()},
        **_target_filter(article, conversation),
    )
    return True


def unlocked_article_ids(user):
    if not user.is_authenticated:
        return set()
    return set(
        ContentUnlock.objects.filter(user=user, article__isnull=False)
                             .values_list('article_id', flat=True)
    )


def unlocked_conversation_ids(user):
    if not user.is_authenticated:
        return set()
    return set(
        ContentUnlock.objects.filter(user=user, conversation__isnull=False)
                             .values_list('conversation_id', flat=True)
    )


# --------------------------------------------------------------------------
# Metered AI usage
# --------------------------------------------------------------------------

def translations_left_today(user):
    """Remaining translation calls, or None for unlimited."""
    if has_premium(user):
        return None
    row = DailyUsage.objects.filter(user=user, date=timezone.localdate()).first()
    used = row.translations if row else 0
    return max(0, FREE_TRANSLATIONS_PER_DAY - used)


def consume_translation(user, count=1):
    """Charge `count` translations against today's budget. False if over limit."""
    if has_premium(user):
        return True

    row, _ = DailyUsage.objects.get_or_create(user=user, date=timezone.localdate())
    if row.translations + count > FREE_TRANSLATIONS_PER_DAY:
        return False

    DailyUsage.objects.filter(pk=row.pk).update(translations=F('translations') + count)
    return True
