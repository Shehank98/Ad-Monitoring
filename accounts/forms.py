from django import forms
from .models import User


class LoginForm(forms.Form):
    email    = forms.EmailField(widget=forms.EmailInput(attrs={
        'class': 'input-field', 'placeholder': 'you@ogilvylk.com', 'autofocus': True}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={
        'class': 'input-field', 'placeholder': '••••••••'}))


class ChangePasswordForm(forms.Form):
    current_password = forms.CharField(widget=forms.PasswordInput(attrs={
        'class': 'input-field', 'placeholder': 'Current password'}))
    new_password     = forms.CharField(widget=forms.PasswordInput(attrs={
        'class': 'input-field', 'placeholder': 'New password (min 8 chars)'}),
        min_length=8)
    confirm_password = forms.CharField(widget=forms.PasswordInput(attrs={
        'class': 'input-field', 'placeholder': 'Confirm new password'}))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('new_password') != cleaned.get('confirm_password'):
            raise forms.ValidationError('New passwords do not match.')
        return cleaned


class CreateUserForm(forms.Form):
    name     = forms.CharField(max_length=150, widget=forms.TextInput(attrs={
        'class': 'input-field', 'placeholder': 'Full name'}))
    email    = forms.EmailField(widget=forms.EmailInput(attrs={
        'class': 'input-field', 'placeholder': 'user@ogilvylk.com'}))
    role     = forms.ChoiceField(choices=[], widget=forms.Select(attrs={'class': 'input-field'}))
    accounts = forms.ModelMultipleChoiceField(
        queryset=None, required=False,
        widget=forms.CheckboxSelectMultiple())
    password = forms.CharField(
        min_length=8,
        widget=forms.TextInput(attrs={
            'class': 'input-field font-mono', 'placeholder': 'Set a temporary password'}))

    def __init__(self, *args, allowed_roles=None, **kwargs):
        from core.models import Account
        super().__init__(*args, **kwargs)
        allowed = allowed_roles or []
        self.fields['role'].choices = [
            (r, label) for r, label in User.ROLES if r in allowed
        ]
        self.fields['accounts'].queryset = Account.objects.all()
