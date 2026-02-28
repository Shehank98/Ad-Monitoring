web: python manage.py migrate --no-input && python manage.py collectstatic --no-input --clear && gunicorn ad_monitor.wsgi --log-file - --bind 0.0.0.0:$PORT --workers 2 --timeout 120
