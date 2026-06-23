#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate
python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username='kevin').exists():
    User.objects.create_superuser('kevin', 'kevinbueno360@gmail.com', 'kevz@rents')
    print('Superuser created')
else:
    print('Superuser already exists')
"