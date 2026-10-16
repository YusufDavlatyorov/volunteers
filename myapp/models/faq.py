"""FAQ page (/faq/): categories of questions, edited in the Django admin.

Translations follow the site's own scheme — there is no gettext and no
modeltranslation: one column per UI language (ru / tj / en, the i18n.js codes).
Russian is required (the server renders Russian, crawlers read it); an empty
Tajik / English column falls back to Russian. The page renders all three and
CSS shows the one matching <html lang>, so the client-side language switch
works without a reload, like every other text on the site.
"""

from django.db import models

FAQ_LANGUAGES = ("ru", "tj", "en")


def _localized(obj, base, lang):
    """`obj.<base>_<lang>`, or the Russian text when that one is empty."""
    value = getattr(obj, f"{base}_{lang}", "") if lang in FAQ_LANGUAGES else ""
    return (value or "").strip() or getattr(obj, f"{base}_ru")


class FAQCategory(models.Model):
    name_ru = models.CharField("Название (RU)", max_length=150)
    name_tj = models.CharField("Название (TJ)", max_length=150, blank=True)
    name_en = models.CharField("Название (EN)", max_length=150, blank=True)
    order = models.PositiveIntegerField("Порядок", default=0, db_index=True)
    is_active = models.BooleanField("Активна", default=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Категория FAQ"
        verbose_name_plural = "Категории FAQ"

    def __str__(self):
        return self.name_ru

    def name(self, lang="ru"):
        return _localized(self, "name", lang)


class FAQItem(models.Model):
    category = models.ForeignKey(
        FAQCategory, on_delete=models.CASCADE, related_name="items", verbose_name="Категория"
    )
    question_ru = models.CharField("Вопрос (RU)", max_length=300)
    question_tj = models.CharField("Вопрос (TJ)", max_length=300, blank=True)
    question_en = models.CharField("Вопрос (EN)", max_length=300, blank=True)
    answer_ru = models.TextField("Ответ (RU)")
    answer_tj = models.TextField("Ответ (TJ)", blank=True)
    answer_en = models.TextField("Ответ (EN)", blank=True)
    order = models.PositiveIntegerField("Порядок", default=0, db_index=True)
    is_active = models.BooleanField("Активен", default=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Вопрос FAQ"
        verbose_name_plural = "Вопросы FAQ"

    def __str__(self):
        return self.question_ru

    def question(self, lang="ru"):
        return _localized(self, "question", lang)

    def answer(self, lang="ru"):
        return _localized(self, "answer", lang)
