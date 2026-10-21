from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from accounts.views import role_redirect, CompulabLoginView, CompulabPasswordResetView, CompulabPasswordResetConfirmView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', role_redirect, name='home'),
    path('accounts/login/', CompulabLoginView.as_view(), name='login'),
    path('accounts/password_reset/', CompulabPasswordResetView.as_view(), name='password_reset'),
    path('accounts/reset/<uidb64>/<token>/', CompulabPasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('accounts/', include('django.contrib.auth.urls')),
    path('accounts/', include('accounts.urls')),
    path('labs/', include('labs.urls')),
    path('schedule/', include('scheduling.urls')),
    path('issues/', include('issues.urls')),
    path('notifications/', include('notifications.urls')),
    path('api/', include('cloud_sync.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)