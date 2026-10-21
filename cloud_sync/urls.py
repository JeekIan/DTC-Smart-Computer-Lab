from django.urls import path

from . import views

urlpatterns = [
    path('ping/', views.ping, name='cloud_sync_ping'),
    path('sync/pull/', views.pull, name='cloud_sync_pull'),
    path('sync/', views.push, name='cloud_sync_push'),
]
