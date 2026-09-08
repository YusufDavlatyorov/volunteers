from django import forms

from accounts.models import REGION_CHOICES
from .models import (
    Broadcast,
    DONATION_CATEGORY_CHOICES,
    DONATION_DONOR_TYPE_CHOICES,
    DONATION_FULFILMENT_CHOICES,
    Event,
    HELP_TYPE_CHOICES,
    HelpRequest,
    MAX_DONATION_QUANTITY,
    PetReport,
    PhotoReport,
    PRIORITY_CHOICES,
    Product,
    STATUS_CHOICES,
)
from .services.geo import is_valid_coordinate


FIELD_CLASS = {
    "class": "control",
}


class HelpRequestForm(forms.ModelForm):
    # Explicit so it can default to "normal" when a caller omits it (the browser
    # select always submits a value; direct form construction in tests may not).
    priority = forms.ChoiceField(
        label="Приоритет",
        choices=PRIORITY_CHOICES,
        required=False,
        widget=forms.Select(attrs=FIELD_CLASS),
    )

    class Meta:
        model = HelpRequest
        fields = ["help_type", "priority", "description", "address", "phone", "latitude", "longitude"]
        labels = {
            "help_type": "Вид помощи",
            "priority": "Приоритет",
            "description": "Описание",
            "address": "Адрес",
            "phone": "Телефон",
        }
        widgets = {
            "help_type": forms.Select(attrs=FIELD_CLASS),
            "priority": forms.Select(attrs=FIELD_CLASS),
            "description": forms.Textarea(attrs={**FIELD_CLASS, "rows": 5, "placeholder": "Что нужно сделать и когда удобно прийти"}),
            "address": forms.TextInput(attrs={**FIELD_CLASS, "placeholder": "Адрес"}),
            "phone": forms.TextInput(attrs={**FIELD_CLASS, "placeholder": "+992 ..."}),
            "latitude": forms.HiddenInput(),
            "longitude": forms.HiddenInput(),
        }

    def clean_priority(self):
        return self.cleaned_data.get("priority") or HelpRequest.PRIORITY_NORMAL

    def save(self, commit=True):
        # Keep the legacy boolean in step with the graded field so existing
        # `.filter(is_urgent=...)` call sites (analytics, CRM filter, matching)
        # stay correct without a second UI control.
        obj = super().save(commit=False)
        obj.priority = self.cleaned_data["priority"]
        obj.is_urgent = obj.priority != HelpRequest.PRIORITY_NORMAL
        if commit:
            obj.save()
        return obj

    def clean(self):
        # DecimalField(max_digits=9) alone allows any value up to ~1000,
        # which is meaningless (and, on the map, misleading) for a real-world
        # WGS84 latitude/longitude — enforce the actual coordinate range here.
        cleaned = super().clean()
        lat, lng = cleaned.get("latitude"), cleaned.get("longitude")
        if (lat is None) != (lng is None):
            raise forms.ValidationError("Укажите широту и долготу вместе или не указывайте вовсе.")
        if lat is not None and lng is not None and not is_valid_coordinate(lat, lng):
            raise forms.ValidationError("Некорректные координаты.")
        return cleaned


class EventForm(forms.ModelForm):
    class Meta:
        model = Event
        fields = ["title", "description", "region", "date"]
        labels = {
            "title": "Название",
            "description": "Описание",
            "region": "Регион",
            "date": "Дата и время",
        }
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
        labels = {
            "subject": "Тема",
            "message": "Сообщение",
            "region": "Регион",
        }
        widgets = {
            "subject": forms.TextInput(attrs={**FIELD_CLASS, "placeholder": "Тема сообщения"}),
            "message": forms.Textarea(attrs={**FIELD_CLASS, "rows": 5, "placeholder": "Текст для волонтеров"}),
            "region": forms.Select(attrs=FIELD_CLASS),
        }


class PhotoReportForm(forms.ModelForm):
    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # A volunteer must not be able to enumerate every HelpRequest in the
        # system through this <select> — its option labels carry the client's
        # username ("<type> для <client>"). Scope it to tasks they are actually
        # attached to; staff keep the full list.
        if user is not None and not (user.is_superuser or user.is_curator):
            self.fields["help_request"].queryset = HelpRequest.objects.filter(volunteer=user)

    class Meta:
        model = PhotoReport
        fields = ["title", "description", "image", "region", "event", "help_request"]
        labels = {
            "title": "Заголовок",
            "description": "Описание",
            "image": "Фото",
            "region": "Регион",
            "event": "Акция",
            "help_request": "Запрос помощи",
        }
        widgets = {
            "title": forms.TextInput(attrs=FIELD_CLASS),
            "description": forms.Textarea(attrs={**FIELD_CLASS, "rows": 4}),
            "region": forms.Select(attrs=FIELD_CLASS, choices=[("", "Выберите регион")] + REGION_CHOICES),
            "event": forms.Select(attrs=FIELD_CLASS),
            "help_request": forms.Select(attrs=FIELD_CLASS),
        }


class PetReportForm(forms.ModelForm):
    """A lost or found pet notice. Location is optional (a hidden map picker, the
    same widget as HelpRequestForm); region is required so a report without
    coordinates still has something the matcher can group on. ``reporter`` and
    ``status`` are set by the view / model, never by the form."""

    region = forms.ChoiceField(
        label="Регион",
        choices=[("", "Выберите регион")] + REGION_CHOICES,
        required=True,
        widget=forms.Select(attrs=FIELD_CLASS),
    )

    class Meta:
        model = PetReport
        fields = [
            "report_type", "pet_name", "species", "breed", "description",
            "region", "contact_phone", "image", "latitude", "longitude",
        ]
        labels = {
            "report_type": "Тип объявления",
            "pet_name": "Кличка (если известна)",
            "species": "Вид животного",
            "breed": "Порода (необязательно)",
            "description": "Описание",
            "contact_phone": "Контактный телефон (необязательно)",
            "image": "Фото (необязательно)",
        }
        widgets = {
            "report_type": forms.Select(attrs=FIELD_CLASS),
            "pet_name": forms.TextInput(attrs={**FIELD_CLASS, "placeholder": "Например: Барсик"}),
            "species": forms.Select(attrs=FIELD_CLASS),
            "breed": forms.TextInput(attrs=FIELD_CLASS),
            "description": forms.Textarea(attrs={**FIELD_CLASS, "rows": 5, "placeholder": "Приметы, где и когда, поведение"}),
            "contact_phone": forms.TextInput(attrs={**FIELD_CLASS, "placeholder": "+992 ..."}),
            "latitude": forms.HiddenInput(),
            "longitude": forms.HiddenInput(),
        }

    def clean(self):
        cleaned = super().clean()
        lat, lng = cleaned.get("latitude"), cleaned.get("longitude")
        if (lat is None) != (lng is None):
            raise forms.ValidationError("Укажите широту и долготу вместе или не указывайте вовсе.")
        if lat is not None and lng is not None and not is_valid_coordinate(lat, lng):
            raise forms.ValidationError("Некорректные координаты.")
        return cleaned


class DonationForm(forms.Form):
    """One in-kind offer of goods — no money. Two shapes: pick a catalogue item
    (its category/unit carry over) or leave it blank and describe the item.
    ``services.donations.create_offer`` is the real validator — this form only
    shapes the input and catches the obvious mistakes early."""

    donor_type = forms.ChoiceField(
        label="Кто передаёт",
        choices=DONATION_DONOR_TYPE_CHOICES,
        initial="individual",
        widget=forms.Select(attrs=FIELD_CLASS),
    )
    organization_name = forms.CharField(
        label="Название организации",
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={**FIELD_CLASS, "placeholder": "Пекарня, магазин, базар…"}),
    )
    product = forms.ModelChoiceField(
        label="Товар из каталога",
        queryset=Product.objects.none(),
        required=False,
        empty_label="Другой предмет (укажу ниже)",
        widget=forms.Select(attrs=FIELD_CLASS),
    )
    item_name = forms.CharField(
        label="Что вы хотите передать",
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={**FIELD_CLASS, "placeholder": "Хлеб, тёплые куртки, вода…"}),
    )
    category = forms.ChoiceField(
        label="Категория",
        choices=DONATION_CATEGORY_CHOICES,
        initial="other",
        widget=forms.Select(attrs=FIELD_CLASS),
    )
    quantity = forms.IntegerField(
        label="Количество",
        required=False,
        min_value=1,
        max_value=MAX_DONATION_QUANTITY,
        initial=1,
        widget=forms.NumberInput(attrs={**FIELD_CLASS, "min": 1}),
    )
    unit = forms.CharField(
        label="Единица",
        required=False,
        max_length=40,
        widget=forms.TextInput(attrs={**FIELD_CLASS, "placeholder": "буханок, наборов, кг…"}),
    )
    description = forms.CharField(
        label="Описание",
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={**FIELD_CLASS, "rows": 3, "placeholder": "Необязательно"}),
    )
    fulfilment = forms.ChoiceField(
        label="Как передать",
        choices=DONATION_FULFILMENT_CHOICES,
        initial="pickup",
        widget=forms.Select(attrs=FIELD_CLASS),
    )
    location = forms.CharField(
        label="Адрес / место получения",
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs=FIELD_CLASS),
    )
    region = forms.ChoiceField(
        label="Регион",
        choices=[("", "—")] + list(REGION_CHOICES),
        required=False,
        widget=forms.Select(attrs=FIELD_CLASS),
    )
    message = forms.CharField(
        label="Сообщение координатору",
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={**FIELD_CLASS, "rows": 2, "placeholder": "Необязательно"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.filter(is_active=True).order_by("name")

    def clean(self):
        cleaned = super().clean()
        product = cleaned.get("product")
        cleaned["quantity"] = cleaned.get("quantity") or 1
        if product:
            cleaned["item_name"] = ""
            cleaned["category"] = product.category
            cleaned["unit"] = cleaned.get("unit") or product.unit
        elif not (cleaned.get("item_name") or "").strip():
            raise forms.ValidationError("Выберите товар из каталога или укажите, что вы хотите передать.")
        if cleaned.get("donor_type") == "business" and not (cleaned.get("organization_name") or "").strip():
            self.add_error("organization_name", "Укажите название организации.")
        return cleaned


class HelpRequestFilterForm(forms.Form):
    help_type = forms.ChoiceField(label="Вид помощи", choices=[("", "Все виды")] + HELP_TYPE_CHOICES, required=False, widget=forms.Select(attrs=FIELD_CLASS))
    region = forms.ChoiceField(label="Регион", choices=[("", "Все регионы")] + REGION_CHOICES, required=False, widget=forms.Select(attrs=FIELD_CLASS))
    priority = forms.ChoiceField(label="Приоритет", choices=[("", "Любой приоритет")] + PRIORITY_CHOICES, required=False, widget=forms.Select(attrs=FIELD_CLASS))
    is_urgent = forms.BooleanField(required=False, label="Только срочные")


class TaskManagementFilterForm(forms.Form):
    """Full filter set for the CRM task-management list — a superset of
    HelpRequestFilterForm's filters, kept separate so the volunteer-facing
    task list (which uses HelpRequestFilterForm) is unaffected."""

    status = forms.ChoiceField(label="Статус", choices=[("", "Все статусы")] + STATUS_CHOICES, required=False, widget=forms.Select(attrs=FIELD_CLASS))
    help_type = forms.ChoiceField(label="Вид помощи", choices=[("", "Все виды")] + HELP_TYPE_CHOICES, required=False, widget=forms.Select(attrs=FIELD_CLASS))
    region = forms.ChoiceField(label="Регион", choices=[("", "Все регионы")] + REGION_CHOICES, required=False, widget=forms.Select(attrs=FIELD_CLASS))
    priority = forms.ChoiceField(label="Приоритет", choices=[("", "Любой приоритет")] + PRIORITY_CHOICES, required=False, widget=forms.Select(attrs=FIELD_CLASS))
    is_urgent = forms.BooleanField(required=False, label="Только срочные")
    volunteer = forms.CharField(
        label="Волонтёр", required=False, widget=forms.TextInput(attrs={**FIELD_CLASS, "placeholder": "Имя волонтёра"})
    )
    date_from = forms.DateField(label="Дата с", required=False, widget=forms.DateInput(attrs={**FIELD_CLASS, "type": "date"}))
    date_to = forms.DateField(label="Дата по", required=False, widget=forms.DateInput(attrs={**FIELD_CLASS, "type": "date"}))
    q = forms.CharField(
        required=False,
        label="Поиск",
        widget=forms.TextInput(attrs={**FIELD_CLASS, "placeholder": "Описание, адрес, клиент, телефон"}),
    )
