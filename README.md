# German Learning Platform

A web app for learning German with real news articles. Articles are fetched
from German news sources, and AI rewrites each one at three levels
(A1/A2, B1/B2, C1). Learners read at their level, save words, and review
them with spaced-repetition flashcards. Translations are shown in the
learner's own language.

**Live demo:** []

## Features
- Articles simplified to A1/A2, B1/B2 and C1, with vocabulary, verbs and grammar notes
- Flashcards using the SM-2 spaced-repetition algorithm
- Translations in 11 languages (English, Arabic, Persian, Turkish, French,
  Spanish, Russian, Chinese, Ukrainian, Polish, Italian)
- AI-generated conversations and grammar lessons (prepositions, noun-verb combinations)
- Staff control panel to fetch and process articles and watch job progress

## Screenshots
![Article page](screenshots/article.png)
![Flashcards](screenshots/flashcards.png)
![Control panel](screenshots/control-panel.png)

## Tech stack
Django 6, SQLite, Groq (Llama 3.3 70B) with Gemini as fallback, NewsAPI

## Run it locally
```bash
git clone https://github.com/Mehdi-mo/gearman-learning-app.git
cd gearman-learning-app
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then fill in your own keys
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Then open http://127.0.0.1:8000/. To get content, log in as staff, open
`/control/`, run "Artikel holen" and then "Artikel verarbeiten".

## Tests
```bash
python manage.py test
```