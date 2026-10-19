from django.urls import path
from . import views

urlpatterns = [
    path('', views.IssuesView.as_view(), name='issues'),
    path('add/', views.IssueCreateView.as_view(), name='issue_create'),
    path('equipment/', views.EquipmentView.as_view(), name='equipment'),
    path('mine/', views.MyIssuesView.as_view(), name='my_issues'),
    path('mine/bulk-delete/', views.my_issues_bulk_delete, name='my_issues_bulk_delete'),
    path('bulk-delete/', views.issues_bulk_delete, name='issues_bulk_delete'),
    path('mine/<int:pk>/', views.MyIssueDetailView.as_view(), name='my_issue_detail'),
    path('mine/<int:pk>/delete/', views.my_issue_delete, name='my_issue_delete'),
    path('<int:pk>/', views.IssueDetailView.as_view(), name='issue_detail'),
    path('<int:pk>/edit/', views.IssueUpdateView.as_view(), name='issue_update'),
    path('<int:pk>/assign/', views.issue_assign, name='issue_assign'),
    path('<int:pk>/resolve/', views.issue_resolve, name='issue_resolve'),
    path('<int:pk>/delete/', views.issue_delete, name='issue_delete'),
]