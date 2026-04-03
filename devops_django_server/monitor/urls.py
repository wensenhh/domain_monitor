from django.urls import path

from . import views

urlpatterns = [
    path("telegram_sender", views.telegram_sender, name="monitor_telegram_sender"),
]

