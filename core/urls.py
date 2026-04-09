from django.urls import path, include
from . import views
from accounts import views as acc_views

urlpatterns = [
    # Dashboard
    path('',                    views.dashboard,          name='dashboard'),

    # User management (admin+)
    path('users/',              acc_views.user_list,      name='user_list'),
    path('users/create/',       acc_views.create_user,    name='create_user'),
    path('users/<int:user_id>/edit/',   acc_views.edit_user,   name='edit_user'),
    path('users/<int:user_id>/delete/', acc_views.delete_user, name='delete_user'),

    # Account management
    path('accounts/',           views.account_list,       name='account_list'),

    # Channel management
    path('channels/',           views.channel_list,       name='channel_list'),

    # Schedules
    path('schedules/',                          views.schedule_list,     name='schedule_list'),
    path('schedules/upload/',                   views.schedule_upload,   name='schedule_upload'),
    path('schedules/detect/',                   views.schedule_detect,   name='schedule_detect'),
    path('schedules/<int:pk>/download/',        views.schedule_download, name='schedule_download'),
    path('schedules/<int:pk>/delete/',          views.schedule_delete,   name='schedule_delete'),
    path('schedules/lock/',                     views.schedule_lock,     name='schedule_lock'),

    # Brand mappings
    path('brand-mappings/',           views.brand_mapping_list,      name='brand_mapping_list'),
    path('brand-mappings/options/',   views.brand_mapping_options,   name='brand_mapping_options'),
    path('brand-mappings/channels/',  views.brand_mapping_channels,  name='brand_mapping_channels'),
    path('brand-mappings/months/',    views.brand_mapping_months,    name='brand_mapping_months'),
    path('brand-mappings/schedules/', views.brand_mapping_schedules, name='brand_mapping_schedules'),

    # Monitoring data
    path('monitoring/',                         views.monitoring_list,     name='monitoring_list'),
    path('monitoring/upload/',                  views.monitoring_upload,   name='monitoring_upload'),
    path('monitoring/detect/',                  views.monitoring_detect,   name='monitoring_detect'),
    path('monitoring/<int:pk>/download/',       views.monitoring_download, name='monitoring_download'),
    path('monitoring/<int:pk>/delete/',                     views.monitoring_delete,       name='monitoring_delete'),
    path('monitoring/group/<str:group_id>/delete/',         views.monitoring_delete_group,  name='monitoring_delete_group'),

    # Monitoring Dashboard (analytics + 7 tabs + PDF)
    path('monitor/',                            views.monitoring_dashboard,   name='monitoring_dashboard'),
    path('monitor/pdf/',                        views.monitoring_pdf,         name='monitoring_pdf'),
    path('monitor/full-report-pdf/',            views.full_ad_report_pdf,     name='full_ad_report_pdf'),
    path('monitor/analytics/',                  views.analytics_full,         name='analytics_full'),

    # Transmission Certificate (TC)
    path('tc/',                                 views.tc_list,      name='tc_list'),
    path('tc/upload/',                          views.tc_upload,    name='tc_upload'),
    path('tc/detect/',                          views.tc_detect,    name='tc_detect'),
    path('tc/preview/',                         views.tc_preview,        name='tc_preview'),
    path('tc/upload-parsed/',                   views.tc_upload_parsed,  name='tc_upload_parsed'),
    path('tc/<int:pk>/delete/',                 views.tc_delete,      name='tc_delete'),
    path('tc/reconcile/',                       views.tc_reconcile,   name='tc_reconcile'),
    path('tc/detail/',                          views.tc_three_way,      name='tc_three_way'),
    path('tc/lmrb-tc/',                         views.lmrb_tc_section,    name='lmrb_tc_section'),
    path('tc/lmrb-candidates/',                 views.tc_lmrb_candidates, name='tc_lmrb_candidates'),
    path('tc/lmrb-link/',                       views.tc_lmrb_link,       name='tc_lmrb_link'),
    path('tc/lmrb-unlink/<int:pk>/',            views.tc_lmrb_unlink,     name='tc_lmrb_unlink'),
    path('tc/pdf-convert/',                     views.tc_pdf_convert,    name='tc_pdf_convert'),

    # Summary Sheet Report
    path('summary/',                            views.summary_report,      name='summary_report'),
    path('summary/excel/',                      views.summary_excel,       name='summary_excel'),
    path('summary/pdf/',                        views.summary_pdf,         name='summary_pdf'),
    path('monitoring/matched-lmrb-excel/',      views.matched_lmrb_excel,  name='matched_lmrb_excel'),
    path('admin-export/',                       views.admin_export,        name='admin_export'),

    # System Settings (super_admin only)
    path('settings/',                           views.system_settings, name='system_settings'),
    path('settings/branding/',                  views.branding_upload, name='branding_upload'),

    # Audit Log (C1)
    path('audit/',                              views.audit_log,       name='audit_log'),

    # Admin Analytics (B1 + B2)
    path('admin/analytics/',                    views.admin_analytics, name='admin_analytics'),

    # Database Tools (admin+)
    path('db-tools/',                           views.db_tools,        name='db_tools'),

    # Sponsorship Reconciliation
    path('sponsorship/reconcile/',  views.sponsorship_reconcile,  name='sponsorship_reconcile'),
    path('sponsorship/candidates/', views.sponsorship_candidates, name='sponsorship_candidates'),
    path('sponsorship/assign/',        views.sponsorship_assign,         name='sponsorship_assign'),
    path('sponsorship/reset/',         views.sponsorship_reset,          name='sponsorship_reset'),
    path('sponsorship/unmatched-rows/',views.sponsorship_unmatched_rows, name='sponsorship_unmatched_rows'),

    # Manual Reconciliation
    path('manual/',                    views.manual_reconciliation, name='manual_reconciliation'),
    path('manual/match/',              views.manual_match_create,   name='manual_match_create'),
    path('manual/dematch/<int:pk>/',   views.manual_dematch,        name='manual_dematch'),

    # Commercial Tags Pool (manual LMRB → commercial schedule assignment)
    path('commercial/candidates/',     views.commercial_candidates,    name='commercial_candidates'),
    path('commercial/unmatched-rows/', views.commercial_unmatched_rows, name='commercial_unmatched_rows'),
    path('commercial/assign/',         views.commercial_assign,         name='commercial_assign'),

    # Competitor Analysis
    path('competitor/', include('competitor.urls')),

    # Channel Officer self-service
    path('channel-officer/',               views.channel_officer_dashboard, name='channel_officer_dashboard'),
    path('tc/<int:pk>/download/',          views.tc_report_download,        name='tc_report_download'),

    # Channel Officers (admin management)
    path('channel-officers/',              views.channel_officer_list,      name='channel_officer_list'),
    path('channel-officers/create/',       views.channel_officer_create,    name='channel_officer_create'),
    path('channel-officers/<int:pk>/edit/', views.channel_officer_edit,     name='channel_officer_edit'),

    # WhatsApp
    path('whatsapp/test/',                 views.whatsapp_test,             name='whatsapp_test'),
    path('whatsapp/tc-reminders/',         views.whatsapp_send_tc_reminders, name='whatsapp_send_tc_reminders'),
    path('whatsapp/webhook/',              views.whatsapp_webhook,           name='whatsapp_webhook'),
]
