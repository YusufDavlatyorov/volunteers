from django import forms

from accounts.form_i18n import localize
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
from .validators import PHONE_PLACEHOLDER, normalize_tj_phone


FIELD_CLASS = {
    "class": "control",
}

# i18n.js keys for the fields several forms share (accounts/form_i18n.py).
DESCRIPTION_I18N = {"label": "form.description"}
REGION_I18N = {"label": "field.region", "choices": "region.", "empty": "choice.select_region"}
HELP_TYPE_I18N = {"label": "form.help_type", "choices": "help."}
PRIORITY_I18N = {"label": "form.priority", "choices": "priority."}
COORDS_PAIR_ERROR = "Укажите широту и долготу вместе или не указывайте вовсе."
COORDS_INVALID_ERROR = "Некорректные координаты."


class TelInput(forms.TextInput):
    """<input type="tel"> with the phone keypad on mobile and the formatting
    hint; base.html's [data-phone-mask] script tidies the number on blur."""

    input_type = "tel"

    def __init__(self, attrs=None):
        super().__init__({
            **FIELD_CLASS,
            "inputmode": "tel",
            "autocomplete": "tel",
            "placeholder": PHONE_PLACEHOLDER,
            "data-phone-mask": "tj",
            **(attrs or {}),
        })


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
            "phone": TelInput(),
            "latitude": forms.HiddenInput(),
            "longitude": forms.HiddenInput(),
        }

    I18N = {
        "help_type": HELP_TYPE_I18N,
        "priority": PRIORITY_I18N,
        "description": {**DESCRIPTION_I18N, "ph": "form.request_desc_ph"},
        "address": {"label": "form.address", "ph": "form.address"},
        "phone": {"label": "form.phone"},
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        localize(self, self.I18N)

    def clean_priority(self):
        return self.cleaned_data.get("priority") or HelpRequest.PRIORITY_NORMAL

    def clean_phone(self):
        return normalize_tj_phone(self.cleaned_data.get("phone"))

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
            raise forms.ValidationError(COORDS_PAIR_ERROR, code="coords_pair")
        if lat is not None and lng is not None and not is_valid_coordinate(lat, lng):
            raise forms.ValidationError(COORDS_INVALID_ERROR, code="coords_invalid")
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        localize(self, {
            "title": {"label": "form.event_title", "ph": "form.event_title_ph"},
            "description": DESCRIPTION_I18N,
            "region": REGION_I18N,
            "date": {"label": "form.datetime"},
        })


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        localize(self, {
            "subject": {"label": "form.subject", "ph": "form.subject_ph"},
            "message": {"label": "form.message", "ph": "form.broadcast_ph"},
            "region": REGION_I18N,
        })


class PhotoReportForm(forms.ModelForm):
    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # A volunteer must not be able to enumerate every HelpRequest in the
        # system through this <select> — its option labels carry the client's
        # username ("<type> для <client>"). Scope it to tasks they are actually
        # attached to; staff keep the full list. select_related: each option
        # label reads task.client (one query per option otherwise).
        help_requests = self.fields["help_request"].queryset.select_related("client")
        if user is not None and not (user.is_superuser or user.is_curator):
            help_requests = help_requests.filter(volunteer=user)
        self.fields["help_request"].queryset = help_requests
        localize(self, {
            "title": {"label": "form.title"},
            "description": DESCRIPTION_I18N,
            "image": {"label": "form.photo"},
            "region": REGION_I18N,
            "event": {"label": "form.event"},
            "help_request": {"label": "form.help_request"},
        })

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
            "contact_phone": TelInput(),
            "latitude": forms.HiddenInput(),
            "longitude": forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        localize(self, {
            "report_type": {"label": "form.pet_report_type", "choices": "pet.type_"},
            "pet_name": {"label": "form.pet_name", "ph": "form.pet_name_ph"},
            "species": {"label": "pet.field_species", "choices": "species."},
            "breed": {"label": "form.breed"},
            "description": {**DESCRIPTION_I18N, "ph": "form.pet_desc_ph"},
            "region": REGION_I18N,
            "contact_phone": {"label": "form.contact_phone"},
            "image": {"label": "form.photo_optional"},
        })

    def clean_contact_phone(self):
        return normalize_tj_phone(self.cleaned_data.get("contact_phone"))

    def clean(self):
        cleaned = super().clean()
        lat, lng = cleaned.get("latitude"), cleaned.get("longitude")
        if (lat is None) != (lng is None):
            raise forms.ValidationError(COORDS_PAIR_ERROR, code="coords_pair")
        if lat is not None and lng is not None and not is_valid_coordinate(lat, lng):
            raise forms.ValidationError(COORDS_INVALID_ERROR, code="coords_invalid")
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
        localize(self, {
            "donor_type": {"label": "form.donor_type", "choices": "don.donor_type_"},
            "organization_name": {"label": "form.org_name", "ph": "form.org_name_ph"},
            "product": {"label": "form.product", "empty": "form.product_other"},
            "item_name": {"label": "form.item_name", "ph": "form.item_name_ph"},
            "category": {"label": "don.col_category", "choices": "don.category_"},
            "quantity": {"label": "form.quantity"},
            "unit": {"label": "form.unit", "ph": "form.unit_ph"},
            "description": {**DESCRIPTION_I18N, "ph": "form.optional_ph"},
            "fulfilment": {"label": "form.fulfilment", "choices": "don.fulfilment_"},
            "location": {"label": "form.location"},
            "region": {"label": "field.region", "choices": "region."},
            "message": {"label": "form.message_coordinator", "ph": "form.optional_ph"},
        })

    def clean(self):
        cleaned = super().clean()
        product = cleaned.get("product")
        cleaned["quantity"] = cleaned.get("quantity") or 1
        if product:
            cleaned["item_name"] = ""
            cleaned["category"] = product.category
            cleaned["unit"] = cleaned.get("unit") or product.unit
        elif not (cleaned.get("item_name") or "").strip():
            raise forms.ValidationError(
                "Выберите товар из каталога или укажите, что вы хотите передать.", code="donation_item_required"
            )
        if cleaned.get("donor_type") == "business" and not (cleaned.get("organization_name") or "").strip():
            self.add_error("organization_name", forms.ValidationError("Укажите название организации.", code="organization_required"))
        return cleaned


class HelpRequestFilterForm(forms.Form):
    help_type = forms.ChoiceField(label="Вид помощи", choices=[("", "Все виды")] + HELP_TYPE_CHOICES, required=False, widget=forms.Select(attrs=FIELD_CLASS))
    region = forms.ChoiceField(label="Регион", choices=[("", "Все регионы")] + REGION_CHOICES, required=False, widget=forms.Select(attrs=FIELD_CLASS))
    priority = forms.ChoiceField(label="Приоритет", choices=[("", "Любой приоритет")] + PRIORITY_CHOICES, required=False, widget=forms.Select(attrs=FIELD_CLASS))
    is_urgent = forms.BooleanField(required=False, label="Только срочные")

    I18N = {
        "help_type": {**HELP_TYPE_I18N, "empty": "choice.any_help_type"},
        "region": {**REGION_I18N, "empty": "choice.any_region"},
        "priority": {**PRIORITY_I18N, "empty": "choice.any_priority"},
        "is_urgent": {"label": "form.urgent_only"},
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        localize(self, self.I18N)


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        localize(self, {
            **HelpRequestFilterForm.I18N,
            "status": {"label": "form.status", "choices": "task_status.", "empty": "choice.any_status"},
            "volunteer": {"label": "form.volunteer", "ph": "form.volunteer_ph"},
            "date_from": {"label": "form.date_from"},
            "date_to": {"label": "form.date_to"},
            "q": {"label": "form.search", "ph": "form.search_ph"},
        })
