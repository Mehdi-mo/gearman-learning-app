from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    path('register/',              views.register,              name='register'),
    path('login/',                 views.login_view,            name='login'),
    path('logout/',                views.logout_view,           name='logout'),

    # Password reset — Django's built-in views. The URL *names* below are the
    # ones Django redirects between internally, so they must stay exactly these.
    path('password-reset/',
         auth_views.PasswordResetView.as_view(
             template_name='accounts/password_reset.html',
             email_template_name='accounts/password_reset_email.txt',
             subject_template_name='accounts/password_reset_subject.txt',
         ),
         name='password_reset'),

    path('password-reset/sent/',
         auth_views.PasswordResetDoneView.as_view(
             template_name='accounts/password_reset_sent.html'),
         name='password_reset_done'),

    path('password-reset/<uidb64>/<token>/',
         auth_views.PasswordResetConfirmView.as_view(
             template_name='accounts/password_reset_confirm.html'),
         name='password_reset_confirm'),

    path('password-reset/complete/',
         auth_views.PasswordResetCompleteView.as_view(
             template_name='accounts/password_reset_done.html'),
         name='password_reset_complete'),
    path('settings/',              views.settings_view,         name='settings'),
    path('upgrade/',               views.upgrade,               name='upgrade'),
    path('saved/',                 views.saved_words,           name='saved_words'),
    path('save-word/',             views.save_word,             name='save_word'),
    path('unsave-word/',           views.unsave_word,           name='unsave_word'),
    path('translate/',             views.translate,             name='translate'),
    path('translate-batch/',       views.translate_batch,       name='translate_batch'),
    path('mark-read/',             views.mark_read,             name='mark_read'),
    path('mark-unread/',           views.mark_unread,           name='mark_unread'),
    path('read-articles/',         views.read_articles,         name='read_articles'),
    path('flashcards/',            views.flashcards,            name='flashcards'),
    path('flashcard-review/',      views.flashcard_review,      name='flashcard_review'),
    path('learn-by-guessing/',     views.learn_by_guessing,     name='learn_by_guessing'),
    path('submit-exercise/',       views.submit_exercise,       name='submit_exercise'),
    path('next-exercise/',         views.next_exercise,         name='next_exercise'),
    path('exercise-review/',       views.exercise_review,       name='exercise_review'),
]