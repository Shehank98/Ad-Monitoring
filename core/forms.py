from django import forms
from .models import Account, Channel, MonitoringData
from datetime import date

MONTHS = [
    f'{date(2024, m, 1).strftime("%B")} {y}'
    for y in range(2024, 2028)
    for m in range(1, 13)
]


class ScheduleUploadForm(forms.Form):
    account         = forms.ModelChoiceField(queryset=Account.objects.all(),
                                              empty_label='Select account…',
                                              widget=forms.Select(attrs={'class': 'select-field'}))
    channel         = forms.ModelChoiceField(queryset=Channel.objects.all(),
                                              empty_label='Select channel…',
                                              widget=forms.Select(attrs={'class': 'select-field'}))
    month           = forms.ChoiceField(choices=[(m, m) for m in MONTHS],
                                        widget=forms.Select(attrs={'class': 'select-field'}))
    schedule_number = forms.CharField(max_length=50,
                                      widget=forms.TextInput(attrs={
                                          'class': 'input-field', 'placeholder': 'e.g. SCH-001'}))
    file            = forms.FileField(widget=forms.FileInput(attrs={
        'class': 'hidden', 'id': 'schedule-file', 'accept': '.xlsx,.xls'}))


class MonitoringUploadForm(forms.Form):
    data_type  = forms.ChoiceField(choices=MonitoringData.DATA_TYPES,
                                   widget=forms.RadioSelect(attrs={'class': 'radio-input'}))
    channel    = forms.ModelChoiceField(queryset=Channel.objects.all(),
                                        empty_label='Select channel…',
                                        widget=forms.Select(attrs={'class': 'select-field'}))
    start_date = forms.DateField(widget=forms.DateInput(attrs={
        'class': 'input-field', 'type': 'date'}))
    end_date   = forms.DateField(widget=forms.DateInput(attrs={
        'class': 'input-field', 'type': 'date'}))
    file       = forms.FileField(widget=forms.FileInput(attrs={
        'class': 'hidden', 'id': 'monitoring-file', 'accept': '.xlsx,.xls'}))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('start_date') and cleaned.get('end_date'):
            if cleaned['start_date'] > cleaned['end_date']:
                raise forms.ValidationError('Start date must be before end date.')
        return cleaned


class AccountForm(forms.ModelForm):
    class Meta:
        model  = Account
        fields = ['name']
        widgets = {'name': forms.TextInput(attrs={
            'class': 'input-field', 'placeholder': 'e.g. Maliban'})}


class ChannelForm(forms.ModelForm):
    class Meta:
        model  = Channel
        fields = ['name']
        widgets = {'name': forms.TextInput(attrs={
            'class': 'input-field', 'placeholder': 'e.g. Sirasa TV'})}
