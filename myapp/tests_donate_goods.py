"""The "Donate goods" page (myapp/donate_goods.html): login/register look,
an ordinary Donation through create_offer, private photos, translated errors."""

import io
import re

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.models import Users
from myapp.models import MAX_DONATION_PHOTOS, Donation, DonationPhoto, HelpRequest, Notification
from myapp.testing import TempMediaRoot

PASSWORD = "Donate-Goods-2026!"
_MEDIA_ROOT = TempMediaRoot("donate-goods-tests-")  # fork-safe (myapp/testing.py)


def _jpeg(name="photo.jpg"):
    buf = io.BytesIO()
    img = Image.new("RGB", (60, 40), (200, 120, 40))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # Make — must not survive the upload
    img.save(buf, "JPEG", exif=exif)
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, **flags
    )


def _payload(**overrides):
    data = {
        "category": "clothing", "item_name": "Тёплые куртки", "description": "Размер 48",
        "quantity": "3", "condition": "used_good", "region": "dushanbe",
        "location": "ул. Рудаки, 10", "fulfilment": "pickup", "contact_phone": "90 123 45 67",
    }
    data.update(overrides)
    return data


@override_settings(MEDIA_ROOT=_MEDIA_ROOT.path)
class DonateGoodsPageTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        _MEDIA_ROOT.cleanup()

    def setUp(self):
        self.donor = _make("gd_donor", is_client=True, region="sogd")
        self.other = _make("gd_other", is_client=True)
        self.volunteer = _make("gd_vol", is_volunteer=True)
        self.curator = _make("gd_curator", is_curator=True)
        self.admin = Users.objects.create_superuser(username="gd_admin", email="gd_admin@example.com", password=PASSWORD)
        self.url = reverse("donate_goods")

    def _login(self, user):
        self.client.login(username=user.username, password=PASSWORD)

    # --- access + look ----------------------------------------------------

    def test_anonymous_is_sent_to_login_with_next(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp.url)
        self.assertIn("next=", resp.url)

    def test_page_uses_the_login_register_scene(self):
        self._login(self.donor)
        html = self.client.get(self.url).content.decode()
        for marker in ('class="auth-shell auth-scene"', "data-auth-bg", 'class="auth-card auth-card--register auth-card--donate',
                       "auth-tokens.css", "auth.css", "auth-bg.js", 'enctype="multipart/form-data"', "auth-submit"):
            self.assertIn(marker, html)

    def test_every_visible_text_is_translated(self):
        """Labels, legends, hints and buttons carry data-i18n keys that exist in
        all three languages of i18n.js."""
        self._login(self.donor)
        html = self.client.get(self.url).content.decode()
        keys = set(re.findall(r'data-i18n="([^"]+)"', html)) | set(re.findall(r'data-i18n-ph="([^"]+)"', html))
        self.assertIn("gd.title", keys)
        with open("static/js/i18n.js", encoding="utf-8") as fh:
            table = fh.read()
        for key in sorted(k for k in keys if k.startswith(("gd.", "err.photo", "err.address"))):
            self.assertEqual(table.count(f'"{key}"'), 3, key)

    def test_region_and_phone_prefilled(self):
        HelpRequest.objects.create(
            client=self.donor, help_type="grocery", description="d", address="a",
            phone="+992901112233", region="sogd",
        )
        self._login(self.donor)
        form = self.client.get(self.url).context["form"]
        self.assertEqual(form["region"].value(), "sogd")
        self.assertEqual(form["contact_phone"].value(), "+992901112233")

    def test_navbar_and_quick_actions_link_here(self):
        self._login(self.donor)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertGreaterEqual(html.count(f'href="{self.url}"'), 2)

    # --- submit -----------------------------------------------------------

    def test_valid_post_creates_pending_offer_and_redirects_to_profile(self):
        self._login(self.donor)
        resp = self.client.post(self.url, _payload(), follow=True)
        self.assertRedirects(resp, reverse("profile"))
        offer = Donation.objects.get(donor=self.donor)
        self.assertEqual(offer.status, Donation.PENDING)
        self.assertEqual((offer.category, offer.quantity, offer.condition), ("clothing", 3, "used_good"))
        self.assertEqual(offer.contact_phone, "+992901234567")
        self.assertEqual(offer.fulfilment, "pickup")
        self.assertEqual(offer.region, "dushanbe")
        self.assertContains(resp, "flash.donation_goods_created")

    def test_staff_are_notified(self):
        self._login(self.donor)
        self.client.post(self.url, _payload())
        recipients = set(Notification.objects.filter(type="donation_new").values_list("recipient_id", flat=True))
        self.assertEqual(recipients, {self.curator.id, self.admin.id})

    def test_errors_are_coded_for_translation(self):
        self._login(self.donor)
        resp = self.client.post(self.url, _payload(category="", item_name="", quantity="0",
                                                   contact_phone="123", condition=""))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Donation.objects.exists())
        html = resp.content.decode()
        for key in ("err.required", "err.min_value", "err.invalid_phone"):
            self.assertIn(f'data-i18n="{key}"', html)

    def test_address_required_only_for_pickup(self):
        self._login(self.donor)
        resp = self.client.post(self.url, _payload(location=""))
        self.assertContains(resp, 'data-i18n="err.address_required"')
        resp = self.client.post(self.url, _payload(location="", fulfilment="dropoff"))
        self.assertEqual(resp.status_code, 302)

    def test_category_outside_the_page_list_is_rejected(self):
        self._login(self.donor)
        resp = self.client.post(self.url, _payload(category="medicine"))
        self.assertContains(resp, 'data-i18n="err.invalid_choice"')

    # --- photos -----------------------------------------------------------

    def test_photos_are_stored_without_metadata_under_random_names(self):
        self._login(self.donor)
        self.client.post(self.url, {**_payload(), "photos": [_jpeg("Zamira_home.jpg"), _jpeg()]})
        photos = list(DonationPhoto.objects.filter(donation__donor=self.donor))
        self.assertEqual(len(photos), 2)
        for photo in photos:
            self.assertTrue(photo.image.name.startswith("donations/"))
            self.assertNotIn("Zamira", photo.image.name)
            with Image.open(photo.image.path) as img:
                self.assertEqual(len(img.getexif()), 0)

    def test_too_many_photos_rejected(self):
        self._login(self.donor)
        files = [_jpeg(f"p{i}.jpg") for i in range(MAX_DONATION_PHOTOS + 1)]
        resp = self.client.post(self.url, {**_payload(), "photos": files})
        self.assertContains(resp, 'data-i18n="err.photos_too_many"')
        self.assertFalse(Donation.objects.exists())

    def test_non_image_rejected(self):
        self._login(self.donor)
        bogus = SimpleUploadedFile("x.jpg", b"not an image", content_type="image/jpeg")
        resp = self.client.post(self.url, {**_payload(), "photos": [bogus]})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Donation.objects.exists())

    def test_photo_view_follows_the_offer_gate(self):
        self._login(self.donor)
        self.client.post(self.url, {**_payload(), "photos": [_jpeg()]})
        photo = DonationPhoto.objects.get()
        url = reverse("donation_photo", args=[photo.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.logout()
        self._login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.logout()
        self._login(self.curator)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Cache-Control"], "private, max-age=3600")

    @override_settings(MEDIA_X_ACCEL=True)
    def test_photo_view_uses_x_accel_in_production(self):
        self._login(self.donor)
        self.client.post(self.url, {**_payload(), "photos": [_jpeg()]})
        photo = DonationPhoto.objects.get()
        resp = self.client.get(reverse("donation_photo", args=[photo.pk]))
        self.assertTrue(resp["X-Accel-Redirect"].startswith("/protected-media/donations/"))

    def test_nginx_refuses_direct_media_urls(self):
        with open("deploy/nginx/khayrkhoh.conf", encoding="utf-8") as fh:
            self.assertIn("location ^~ /media/donations/ { return 404; }", fh.read())

    # --- admin ------------------------------------------------------------

    def test_admin_lists_and_filters_offers(self):
        self._login(self.donor)
        self.client.post(self.url, {**_payload(), "photos": [_jpeg()]})
        offer = Donation.objects.get()
        self.client.logout()
        self._login(self.admin)
        changelist = reverse("admin:myapp_donation_changelist")
        for query in ("status__exact=pending", "category__exact=clothing", "region__exact=dushanbe", "condition__exact=used_good"):
            resp = self.client.get(f"{changelist}?{query}")
            self.assertEqual(resp.status_code, 200, query)
            self.assertContains(resp, f"/{offer.pk}/change/")
        resp = self.client.get(reverse("admin:myapp_donation_change", args=[offer.pk]))
        self.assertContains(resp, reverse("donation_photo", args=[offer.photos.get().pk]))
