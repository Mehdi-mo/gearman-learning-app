from django.urls import path
from . import views

urlpatterns = [
    path('',                             views.home,                  name='home'),
    path('articles/',                    views.article_list,          name='article_list'),
    path('article/<int:pk>/',            views.article_detail,        name='article_detail'),
    path('conversations/',               views.conversation_list,     name='conversation_list'),
    path('conversations/<int:pk>/',      views.conversation_detail,   name='conversation_detail'),
    path('grammar/prepositions/',        views.preposition_lesson,    name='preposition_lesson'),
    path('grammar/nomen-verb/',          views.nomen_verb_lesson,     name='nomen_verb_lesson'),
]