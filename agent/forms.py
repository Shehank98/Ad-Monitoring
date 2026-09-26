"""Admin forms for Agent Settings (Phase 2). Saved through gate.perform(actor_kind='human')."""
from django import forms
from django.contrib.auth import get_user_model

from core.models import Account
from intake.models import AllowedSender

from .models import AUTONOMY_NOTE, MAX_AUTONOMY_LEVEL, AgentConfig


class AgentConfigForm(forms.ModelForm):
    tc_intake_mode = forms.ChoiceField(choices=AgentConfig.INTAKE_MODES)   # 'auto' is not a choice
    digest_recipients = forms.ModelMultipleChoiceField(
        queryset=get_user_model().objects.filter(is_active=True, role__in=('super_admin', 'admin')).order_by('email'),
        required=False, help_text='Empty = every active super admin and admin.')

    class Meta:
        model = AgentConfig
        fields = ['enabled', 'autonomy_level', 'mapping_threshold', 'grace_days', 'upload_debounce_minutes',
                  'tc_intake_mode', 'intake_fetch_enabled', 'intake_gemini_enabled', 'min_brand_overlap',
                  'llm_daily_token_cap',
                  # Phase 3
                  'shadow_window_start', 'shadow_window_end', 'shadow_budget_seconds', 'max_scopes_per_cycle',
                  'observe_every_minutes', 'db_lock_timeout_ms', 'db_statement_timeout_ms', 'db_idle_timeout_ms',
                  'core_fingerprint_timeout_ms', 'digest_time', 'digest_recipients',
                  # Phase 3.2 close
                  'intake_tool_choice']
        widgets = {'shadow_window_start': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
                   'shadow_window_end': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
                   'digest_time': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M')}
        labels = {'shadow_window_start': 'Shadow window start (Colombo)',
                  'shadow_window_end': 'Shadow window end (Colombo)',
                  'shadow_budget_seconds': 'Shadow budget per night (s)',
                  'observe_every_minutes': 'Observe each scope every (minutes)',
                  'db_lock_timeout_ms': 'DB lock timeout (ms)', 'db_statement_timeout_ms': 'DB statement timeout (ms)',
                  'db_idle_timeout_ms': 'DB idle-in-transaction timeout (ms)',
                  'core_fingerprint_timeout_ms': 'Core fingerprint timeout per table (ms)',
                  'digest_time': 'Daily digest time (Colombo)'}

    def __init__(self, data=None, *args, **kwargs):
        """Phase 3.1 T10: a field missing from the POST keeps its stored value, so a form posted by an
        older page or test (without the fields added later) never resets or fails them. Exception:
        checkboxes. A missing checkbox always means False (the kill switch fails safe)."""
        super().__init__(data, *args, **kwargs)
        if data is None or not self.instance.pk:
            return
        rendered = {x for x in (data.get('_fields') or '').split(',') if x}
        filled = data.copy()
        multi = hasattr(filled, 'setlist')
        for name, field in self.fields.items():
            if name in data or name in rendered:
                continue
            value = getattr(self.instance, name)
            if name == 'digest_recipients':
                pks = [str(pk) for pk in value.values_list('pk', flat=True)]
                if multi:
                    filled.setlist(name, pks)
                else:
                    filled[name] = pks
            elif isinstance(field, forms.BooleanField):
                # Fail safe (guardian 3.1): a checkbox the person did not send is always False, so a
                # stale page can never keep the kill switch (or any other switch) on.
                continue
            elif hasattr(value, 'strftime'):
                filled[name] = value.strftime('%H:%M')
            elif value is not None:
                filled[name] = str(value)
        self.data = filled

    def clean(self):
        d = super().clean()
        if d.get('shadow_window_start') and d.get('shadow_window_start') == d.get('shadow_window_end'):
            raise forms.ValidationError('The shadow window start and end must differ.')
        for k in ('db_lock_timeout_ms', 'db_statement_timeout_ms', 'db_idle_timeout_ms', 'core_fingerprint_timeout_ms'):
            if d.get(k) is not None and d[k] < 100:
                self.add_error(k, 'Use at least 100 ms.')
        return d

    def clean_intake_tool_choice(self):
        """'forced' only if the latest intake_llm_probe of the current model showed forced tool_choice
        as supported. Checked when an admin switches to forced; the runner re-checks on every run."""
        v = self.cleaned_data['intake_tool_choice']
        if v == 'forced' and getattr(self.instance, 'intake_tool_choice', 'auto') != 'forced':
            from intake.llm.probe import current_model, forced_supported
            ok, why = forced_supported(current_model())
            if not ok:
                raise forms.ValidationError(f'Forced tool choice is not allowed: {why}.')
        return v

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
