from django import forms
from .models import Account, Channel, Client, MonitoringData


class ScheduleUploadForm(forms.Form):
    """Month is removed — auto-detected from the Date column of the uploaded file."""
    account         = forms.ModelChoiceField(
        queryset=Account.objects.all(),
        empty_label='Select account…',
        widget=forms.Select(attrs={'class': 'select-field'}),
    )
    channel         = forms.ModelChoiceField(
        queryset=Channel.objects.all(),
        empty_label='Select channel…',
        widget=forms.Select(attrs={'class': 'select-field'}),
    )
    schedule_number = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={'class': 'input-field', 'placeholder': 'e.g. SCH-001'}),
    )
    file            = forms.FileField(
        widget=forms.FileInput(attrs={'class': 'hidden', 'id': 'schedule-file', 'accept': '.xlsx,.xls'}),
    )


class MonitoringUploadForm(forms.Form):
    """Channel, start_date and end_date removed — all auto-detected from the uploaded file.

    For LMRB / MapOnline files that contain multiple channels, one MonitoringData record
    is created per detected channel (all sharing the same physical file).
    """
    account   = forms.ModelChoiceField(
        queryset=Account.objects.none(),
        empty_label='Select account…',
        widget=forms.Select(attrs={'class': 'select-field'}),
    )
    data_type = forms.ChoiceField(
        choices=MonitoringData.DATA_TYPES,
        widget=forms.RadioSelect(attrs={'class': 'radio-input'}),
    )
    file      = forms.FileField(
        widget=forms.FileInput(attrs={'class': 'hidden', 'id': 'monitoring-file', 'accept': '.xlsx,.xls'}),
    )

    def __init__(self, *args, account_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if account_queryset is not None:
            self.fields['account'].queryset = account_queryset
        else:
            self.fields['account'].queryset = Account.objects.all()


class ClientForm(forms.ModelForm):
    class Meta:
        model  = Client
        fields = ['name']
        widgets = {'name': forms.TextInput(attrs={
            'class': 'input-field', 'placeholder': 'e.g. Nestlé / SLT-Mobitel'})}


class AccountForm(forms.ModelForm):
    class Meta:
        model  = Account
        fields = ['name', 'client']
        widgets = {
            'name':   forms.TextInput(attrs={
                'class': 'input-field', 'placeholder': 'e.g. Maliban'}),
            'client': forms.Select(attrs={'class': 'select-field'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['client'].required = False
        self.fields['client'].empty_label = 'No client (ungrouped)'


class ChannelForm(forms.ModelForm):
    class Meta:
        model  = Channel
        fields = ['name']
        widgets = {'name': forms.TextInput(attrs={
            'class': 'input-field', 'placeholder': 'e.g. Sirasa TV'})}
