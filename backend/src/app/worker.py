from celery import Celery

from app.config import Settings

settings = Settings()  # type: ignore[call-arg]

celery_app = Celery("flowbuilder", broker=settings.redis_url, backend=settings.redis_url)
