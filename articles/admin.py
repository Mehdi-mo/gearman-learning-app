from django.contrib import admin
from .models import Article, Conversation, PrepositionLesson, PrepositionExercise, NomenVerbVerbindung, NomenVerbExercise

@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display  = ['title','category','is_processed','published_at']
    list_filter   = ['category','is_processed']
    search_fields = ['title']

@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display  = ['topic','level','created_at']
    list_filter   = ['level']
    search_fields = ['topic']

@admin.register(PrepositionLesson)
class PrepositionLessonAdmin(admin.ModelAdmin):
    list_display = ['preposition','use_case','created_at']

@admin.register(PrepositionExercise)
class PrepositionExerciseAdmin(admin.ModelAdmin):
    list_display = ['sentence','correct','category']

@admin.register(NomenVerbVerbindung)
class NomenVerbAdmin(admin.ModelAdmin):
    list_display  = ['collocation','verb','noun']
    search_fields = ['collocation']

@admin.register(NomenVerbExercise)
class NomenVerbExerciseAdmin(admin.ModelAdmin):
    list_display = ['sentence','correct','collocation']