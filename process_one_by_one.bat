@echo off
cd C:\django_projects\german-learning
call venv\Scripts\activate.bat

:loop
echo.
echo Processing one article...
python manage.py process_articles
echo.
echo Waiting 90 seconds before next article...
timeout /t 90 /nobreak
goto loop