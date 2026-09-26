"""Admin forms for Agent Settings (Phase 2). Saved through gate.perform(actor_kind='human')."""
from django import forms

from core.models import Account
from intake.models import AllowedSender

from .models import AUTONOMY_NOTE, MAX_AUTONOMY_LEVEL, AgentConfig


class AgentConfigForm(forms.ModelForm):
    tc_intake_mode = forms.ChoiceField(choices=AgentConfig.INTAKE_MODES)   # 'auto' is not a choice

    class Meta:
        model = AgentConfig
        fields = ['enabled', 'autonomy_level', 'mapping_threshold', 'grace_days', 'upload_debounce_minutes',
                  'tc_intake_mode', 'intake_fetch_enabled', 'intake_gemini_enabled', 'min_brand_overlap',
                  'llm_daily_token_cap']

    def clean_autonomy_level(self):
        v = self.cleaned_data['autonomy_level']
        if v > MAX_AUTONOMY_LEVEL:                      # Phase 2.1 item 7
            raise forms.ValidationError(AUTONOMY_NOTE)
        return v

    def clean_min_brand_overlap(self):
        v = self.cleaned_data['min_brand_overlap']
        if not 0 < v <= 1:
            raise forms.ValidationError('Use a share between 0 and 1, e.g. 0.6.')
        return v


class AllowedSenderForm(forms.ModelForm):
    accounts = forms.ModelMultipleChoiceField(queryset=Account.objects.order_by('name'), required=False,
                                              help_text='Leave empty for all clients.')

    class Meta:
        model = AllowedSender
        fields = ['email_or_domain', 'channel_hint', 'accounts', 'note']

    def clean_email_or_domain(self):
        v = self.cleaned_data['email_or_domain'].strip().lower()
        if not v or ' ' in v or v.count('@') > 1 or v.startswith('@') or '.' not in v.rpartition('@')[2]:
            raise forms.ValidationError('Enter an exact address (desk@tv.lk) or a domain (tv.lk).')
        return v


class OverrideForm(forms.Form):
    account = forms.ModelChoiceField(queryset=Account.objects.order_by('name'))
    enabled = forms.TypedChoiceField(choices=[('', 'Use global'), ('1', 'On'), ('0', 'Off')], required=False,
                                     coerce=lambda v: {'1': True, '0': False}.get(v), empty_value=None)
    autonomy_level = forms.TypedChoiceField(choices=[('', 'Use global'), *[(str(i), str(i)) for i in range(4)]],
                                            required=False, coerce=int, empty_value=None)
