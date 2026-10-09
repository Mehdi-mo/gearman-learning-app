# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Activate virtual environment (Windows)
venv\Scripts\activate

# Run development server
python manage.py runserver

# Run all tests
python manage.py test

# Run tests for a specific app
python manage.py test accounts
python manage.py test articles

# Apply migrations
python manage.py migrate

# Create new migrations
python manage.py makemigrations

# Management commands (content pipeline)
python manage.py fetch_articles --pages 2          # Fetch German news from NewsAPI
python manage.py process_articles --all            # AI-generate simplified versions for all unprocessed articles
python manage.py generate_conversations --level all --count 5
python manage.py generate_grammar_content

# Django shell
python manage.py shell
```

## Architecture Overview

This is a German language learning platform where content is AI-generated and adapted to learner levels.

### Content Pipeline

Articles flow through a pipeline:
1. **fetch_articles** pulls German news from NewsAPI (Spiegel, Die Zeit, Handelsblatt, etc.)
2. **process_articles** sends each article to AI to generate simplified versions at three proficiency bands (A1/A2, B1/B2, C1) and extract vocabulary, verbs, and grammar
3. Users read the version matching their level, save words, and review them via flashcards

AI calls go through `articles/ai_processor.py`, which tries Groq (Llama 3.3 70B) first and falls back to Gemini on rate limits. Both keys are loaded from `.env`.

### Spaced Repetition

`accounts/models.py` — `SavedWord` implements the SM-2 algorithm. Fields: `ease_factor`, `interval`, `repetitions`, `next_review`, `last_reviewed`, `times_correct`, `times_wrong`. The review logic lives in `accounts/views.py` (flashcard review view).

### Apps

- **accounts** — auth, `UserProfile` (native language + proficiency level), `SavedWord` (vocab + SM-2), `ReadArticle` (reading history)
- **articles** — `Article` (multi-level content, JSONFields for vocab/grammar), `Conversation` (AI dialogues), `PrepositionLesson`/`NomenVerbVerbindung` (static grammar), and corresponding exercise models

### URL Structure

```
/                    → articles: article list, detail, conversations, grammar lessons
/accounts/           → auth + JSON POST endpoints (save-word, translate, translate-batch)
/admin/              → Django admin
```

The translate/save-word endpoints (`/accounts/translate/`, `/accounts/translate-batch/`) accept JSON POST and return JSON — they're called from JavaScript in templates.

### AI Integration

`articles/ai_processor.py` exports `ask_ai(prompt)` which handles provider fallback. Direct Groq and Gemini clients are also available as `ask_groq()` and `ask_gemini()`. Rate limit errors from Groq trigger an automatic retry via Gemini.

### Configuration

- Django 6.0.5, SQLite3 (`db.sqlite3`), DEBUG=True
- API keys: `GROQ_API_KEY`, `GEMINI_API_KEY`, `NEWS_API_KEY` — loaded from `.env` via python-dotenv
- Supported native languages for translation: English, Arabic, Persian, Turkish, French, Spanish, Russian, Chinese, Ukrainian, Polish, Italian
- German levels: A1/A2, B1/B2, C1


## Testing rule

After adding or changing any feature, launch the django-tester agent to write tests
for it, then run `python manage.py test` and report the result before saying the work is done.

## Design first

Before building any new feature, follow the design-first skill: ask me multiple-choice
questions, show the design, and wait for my approval before changing any files.