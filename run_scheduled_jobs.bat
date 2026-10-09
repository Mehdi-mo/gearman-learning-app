@echo off
REM Runs any content jobs that are due. Called by Windows Task Scheduler.
REM Works whether or not the Django server itself is running.
cd /d C:\django_projects\german-learning
call venv\Scripts\activate.bat
python manage.py run_due_jobs >> logs\scheduler.log 2>&1
