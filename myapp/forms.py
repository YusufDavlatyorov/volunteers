from django import forms

from accounts.models import REGION_CHOICES
from .models import Broadcast, Event, HELP_TYPE_CHOICES, HelpRequest, PhotoReport


FIELD_CLASS = {
    "class": "control",
}


class HelpRequestForm(forms.ModelForm):
    class Meta:
        model = HelpRequest
        fields = ["help_type", "description", "address", "phone", "is_urgent"]
        widgets = {
            "help_type": forms.Select(attrs=FIELD_CLASS),
            "description": forms.Textarea(attrs={**FIELD_CLASS, "rows": 5, "placeholder": "Что нужно сделать и когда удобно прийти"}),
            "address": forms.TextInput(attrs={**FIELD_CLASS, "placeholder": "Адрес"}),
            "phone": forms.TextInput(attrs={**FIELD_CLASS, "placeholder": "+992 ..."}),
            "is_urgent": forms.CheckboxInput(),
        }


class EventForm(forms.ModelForm):
    class Meta:
        model = Event
        fields = ["title", "description", "region", "date"]
        widgets = {
            "title": forms.TextInput(attrs={**FIELD_CLASS, "placeholder": "Например: субботник в парке"}),
            "description": forms.Textarea(attrs={**FIELD_CLASS, "rows": 5}),
            "region": forms.Select(attrs=FIELD_CLASS),
            "date": forms.DateTimeInput(attrs={**FIELD_CLASS, "type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        }


class BroadcastForm(forms.ModelForm):
    class Meta:
        model = Broadcast
        fields = ["subject", "message", "region"]
        widgets = {
            "subject": forms.TextInput(attrs={**FIELD_CLASS, "placeholder": "Тема сообщения"}),
            "message": forms.Textarea(attrs={**FIELD_CLASS, "rows": 5, "placeholder": "Текст для волонтеров"}),
            "region": forms.Select(attrs=FIELD_CLASS),
        }


class PhotoReportForm(forms.ModelForm):
    class Meta:
        model = PhotoReport
        fields = ["title", "description", "image", "region", "event", "help_request"]
        widgets = {
            "title": forms.TextInput(attrs=FIELD_CLASS),
            "description": forms.Textarea(attrs={**FIELD_CLASS, "rows": 4}),
            "region": forms.Select(attrs=FIELD_CLASS, choices=[("", "Выберите регион")] + REGION_CHOICES),
            "event": forms.Select(attrs=FIELD_CLASS),
            "help_request": forms.Select(attrs=FIELD_CLASS),
        }


class HelpRequestFilterForm(forms.Form):
    help_type = forms.ChoiceField(choices=[("", "Все виды")] + HELP_TYPE_CHOICES, required=False, widget=forms.Select(attrs=FIELD_CLASS))
    region = forms.ChoiceField(choices=[("", "Все регионы")] + REGION_CHOICES, required=False, widget=forms.Select(attrs=FIELD_CLASS))
    is_urgent = forms.BooleanField(required=False, label="Только срочные")
