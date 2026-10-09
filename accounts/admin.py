from django.contrib import admin
from .models import UserProfile, SavedWord, ReadArticle, ExerciseAttempt, ContentUnlock, DailyUsage, TranslationCache

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display  = ['user', 'native_language', 'preferred_level', 'is_premium', 'premium_until']
    list_editable = ['is_premium', 'premium_until']
    list_filter   = ['is_premium', 'preferred_level']
    search_fields = ['user__username']

@admin.register(SavedWord)
class SavedWordAdmin(admin.ModelAdmin):
    list_display = ['user', 'word', 'word_type', 'saved_at']

@admin.register(ReadArticle)
class ReadArticleAdmin(admin.ModelAdmin):
    list_display = ['user', 'article', 'conversation', 'read_at']

@admin.register(ExerciseAttempt)
class ExerciseAttemptAdmin(admin.ModelAdmin):
    list_display = ['user', 'exercise', 'is_correct', 'times_correct', 'times_wrong', 'next_review']


@admin.register(ContentUnlock)
class ContentUnlockAdmin(admin.ModelAdmin):
    list_display  = ['user', 'article', 'conversation', 'unlocked_on']
    list_filter   = ['unlocked_on']
    search_fields = ['user__username']


@admin.register(DailyUsage)
class DailyUsageAdmin(admin.ModelAdmin):
    list_display  = ['user', 'date', 'translations']
    list_filter   = ['date']
    search_fields = ['user__username']


@admin.register(TranslationCache)
class TranslationCacheAdmin(admin.ModelAdmin):
    list_display  = ['source_text', 'source_lang', 'target_lang', 'translation', 'hit_count', 'last_used']
    list_filter   = ['source_lang', 'target_lang']
    search_fields = ['source_text', 'translation']
    readonly_fields = ['source_hash', 'hit_count', 'created_at', 'last_used']
