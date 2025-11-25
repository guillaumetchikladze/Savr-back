import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'savr_back.settings')

app = Celery('savr_back')
app.config_from_object('django.conf:settings', namespace='CELERY')
# Découvrir automatiquement les tâches dans toutes les apps Django
app.autodiscover_tasks()


@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')

