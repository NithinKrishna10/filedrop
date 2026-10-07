import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'filedrop.settings')

application = get_wsgi_application()

# Vercel's python runtime looks for `app`; other hosts use `application`.
app = application
