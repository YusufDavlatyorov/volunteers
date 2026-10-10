"""Milestone 6 — speed + CDN readiness.

* ``server/client_ip.py``: one trusted client-IP helper for every rate limiter
  (nginx X-Real-IP, Cloudflare CF-Connecting-IP only from Cloudflare ranges).
"""

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts.models import Users
from server.client_ip import client_ip, is_cloudflare

PRIVATE_PEER = "172.18.0.1"
CF_EDGE = "162.158.10.20"
VISITOR = "95.142.80.7"
OTHER_VISITOR = "95.142.80.8"


class ClientIpTests(SimpleTestCase):
    def ip(self, **meta):
        meta.setdefault("REMOTE_ADDR", PRIVATE_PEER)
        return client_ip(RequestFactory().get("/", **meta))

    @override_settings(BEHIND_TLS_PROXY=False, BEHIND_CLOUDFLARE=False)
    def test_direct_connection_uses_peer_and_ignores_headers(self):
        self.assertEqual(self.ip(REMOTE_ADDR=VISITOR, HTTP_X_REAL_IP="1.2.3.4"), VISITOR)

    @override_settings(BEHIND_TLS_PROXY=True, BEHIND_CLOUDFLARE=False)
    def test_behind_nginx_uses_x_real_ip(self):
        self.assertEqual(self.ip(HTTP_X_REAL_IP=VISITOR), VISITOR)
        self.assertEqual(self.ip(REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP=VISITOR), VISITOR)

    @override_settings(BEHIND_TLS_PROXY=True, BEHIND_CLOUDFLARE=False)
    def test_x_real_ip_from_a_public_peer_is_not_trusted(self):
        self.assertEqual(self.ip(REMOTE_ADDR=VISITOR, HTTP_X_REAL_IP="10.0.0.9"), VISITOR)

    @override_settings(BEHIND_TLS_PROXY=True, BEHIND_CLOUDFLARE=False)
    def test_garbage_header_falls_back_to_peer(self):
        self.assertEqual(self.ip(HTTP_X_REAL_IP="not-an-ip"), PRIVATE_PEER)
        self.assertEqual(self.ip(REMOTE_ADDR=""), "unknown")

    @override_settings(BEHIND_TLS_PROXY=True, BEHIND_CLOUDFLARE=False)
    def test_cf_header_ignored_unless_cloudflare_enabled(self):
        self.assertEqual(self.ip(HTTP_X_REAL_IP=CF_EDGE, HTTP_CF_CONNECTING_IP=VISITOR), CF_EDGE)

    @override_settings(BEHIND_TLS_PROXY=True, BEHIND_CLOUDFLARE=True)
    def test_cloudflare_edge_passes_the_visitor(self):
        self.assertEqual(self.ip(HTTP_X_REAL_IP=CF_EDGE, HTTP_CF_CONNECTING_IP=VISITOR), VISITOR)
        self.assertEqual(self.ip(HTTP_X_REAL_IP="2606:4700::1", HTTP_CF_CONNECTING_IP="2a02:2698::5"), "2a02:2698::5")

    @override_settings(BEHIND_TLS_PROXY=True, BEHIND_CLOUDFLARE=True)
    def test_forged_cf_header_from_outside_cloudflare_is_ignored(self):
        # Someone bypassing Cloudflare talks to nginx directly with a forged header.
        self.assertEqual(self.ip(HTTP_X_REAL_IP=VISITOR, HTTP_CF_CONNECTING_IP="8.8.8.8"), VISITOR)

    def test_ranges(self):
        self.assertTrue(is_cloudflare("104.16.1.1"))
        self.assertTrue(is_cloudflare("2400:cb00::1"))
        self.assertFalse(is_cloudflare("95.142.80.7"))
        self.assertFalse(is_cloudflare("nonsense"))


@override_settings(BEHIND_TLS_PROXY=True, BEHIND_CLOUDFLARE=False, TELEGRAM_BOT_TOKEN="")
class LoginThrottlePerVisitorTests(TestCase):
    """Regression: behind nginx every visitor used to share the proxy peer's
    throttle bucket, so one person's failed logins locked everyone out."""

    def setUp(self):
        cache.clear()
        Users.objects.create_user(username="m6_user", email="m6@example.com", password="Right-Pass-2026!")

    def post(self, ip, password):
        return self.client.post(reverse("login"), {"username": "m6_user", "password": password},
                                REMOTE_ADDR=PRIVATE_PEER, HTTP_X_REAL_IP=ip)

    def test_one_visitors_failures_do_not_block_another(self):
        from accounts import views as account_views

        for _ in range(account_views.LOGIN_IP_ATTEMPT_LIMIT):
            cache_key = f"login_attempts:ip:{VISITOR}"
            cache.set(cache_key, cache.get(cache_key, 0) + 1, 900)
        blocked = self.post(VISITOR, "Right-Pass-2026!")
        self.assertContains(blocked, "Слишком много попыток входа")
        ok = self.post(OTHER_VISITOR, "Right-Pass-2026!")
        self.assertEqual(ok.status_code, 302)


# --------------------------------------------------------------------------- #
# WebP variants + metadata stripping (avatars / photo reports / pet photos)
# --------------------------------------------------------------------------- #

import io  # noqa: E402
import os  # noqa: E402
from unittest import mock  # noqa: E402

from django.core.files.base import ContentFile  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.template import Context, Template  # noqa: E402
from PIL import Image  # noqa: E402

from accounts.models import Profile  # noqa: E402
from myapp.testing import TempMediaRoot
from myapp.models import ChatMessage, HelpRequest, PetReport, PhotoReport  # noqa: E402
from myapp.services import chat, images  # noqa: E402

_MEDIA_ROOT = TempMediaRoot("gc-m6-tests-")  # fork-safe (myapp/testing.py)
_MEDIA = _MEDIA_ROOT.path
PASSWORD = "M6-Speed-2026!"
GPS_TAG = 34853


def tearDownModule():
    _MEDIA_ROOT.cleanup()


def jpeg_with_gps(size=(1600, 1200), name="Ivanova Maria home.jpg"):
    img = Image.new("RGB", size, (120, 160, 200))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # Make
    exif[GPS_TAG] = {1: "N", 2: (38.0, 33.0, 36.0), 3: "E", 4: (68.0, 47.0, 13.0)}
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85, exif=exif)
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


def stored_exif(field_file):
    field_file.open("rb")
    try:
        with Image.open(field_file) as img:
            return dict(img.getexif()), img.info.get("exif")
    finally:
        field_file.close()


@override_settings(MEDIA_ROOT=_MEDIA, TELEGRAM_BOT_TOKEN="")
class ImageUploadTests(TestCase):
    def setUp(self):
        cache.clear()
        self.vol = Users.objects.create_user(username="m6_vol", email="m6v@example.com", password=PASSWORD,
                                             region="dushanbe", is_volunteer=True)
        self.client.force_login(self.vol)

    def assert_clean_with_variants(self, obj):
        obj.refresh_from_db()
        exif, raw = stored_exif(obj.image)
        self.assertNotIn(GPS_TAG, exif)
        self.assertIsNone(raw)
        self.assertNotIn("Ivanova", obj.image.name)  # random name, not the user's file name
        self.assertTrue(images.variants_current(obj.image, obj.image_variants))
        for name, px in images.DEFAULT_SIZES.items():
            obj.image_variants[name]
            with obj.image.storage.open(obj.image_variants[name]) as fh, Image.open(fh) as v:
                self.assertEqual(v.format, "WEBP")
                self.assertLessEqual(max(v.size), px)
                self.assertFalse(v.getexif())

    def test_clean_original_drops_gps(self):
        clean, err = images.clean_original(jpeg_with_gps())
        self.assertIsNone(err)
        self.assertRegex(clean.name, r"^[0-9a-f]{32}\.jpg$")
        with Image.open(io.BytesIO(clean.read())) as img:
            self.assertNotIn(GPS_TAG, img.getexif())

    def test_avatar_upload_endpoint(self):
        response = self.client.post(reverse("update_profile"), {"image": jpeg_with_gps()})
        self.assertEqual(response.json(), {"success": True})
        self.assert_clean_with_variants(Profile.objects.get(user=self.vol))

    def test_avatar_upload_rejects_non_image_without_partial_write(self):
        bogus = SimpleUploadedFile("x.jpg", b"not an image", content_type="image/jpeg")
        response = self.client.post(reverse("update_profile"), {"image": bogus, "region": "sogd"})
        self.assertEqual(response.status_code, 400)
        self.vol.refresh_from_db()
        self.assertEqual(self.vol.region, "dushanbe")

    def test_edit_profile_form_upload(self):
        response = self.client.post(reverse("edit_profile"), {
            "email": self.vol.email, "region": "dushanbe", "full_name": "Test", "image": jpeg_with_gps(),
        })
        self.assertEqual(response.status_code, 302, response.context and response.context["profile_form"].errors)
        self.assert_clean_with_variants(Profile.objects.get(user=self.vol))

    def test_photo_report_upload_and_public_markup(self):
        response = self.client.post(reverse("photo_reports"), {"title": "Субботник", "description": "d",
                                                                "region": "dushanbe", "image": jpeg_with_gps()})
        self.assertEqual(response.status_code, 302)
        report = PhotoReport.objects.get()
        self.assert_clean_with_variants(report)
        self.client.logout()
        cache.clear()
        html = self.client.get(reverse("about")).content.decode()
        self.assertIn('<picture><source type="image/webp" srcset="', html)
        self.assertIn(report.image_variants["thumb"].split("/")[-1] + " 320w", html)
        self.assertIn(f'src="{report.image.url}"', html)  # original stays the fallback
        self.assertIn('class="report-img"', html)

    def test_pet_create_and_edit(self):
        response = self.client.post(reverse("pet_report_create"), {
            "report_type": "lost", "species": "dog", "description": "brown dog", "region": "dushanbe",
            "image": jpeg_with_gps(),
        })
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["form"].errors)
        report = PetReport.objects.get()
        self.assert_clean_with_variants(report)
        old_thumb = report.image_variants["thumb"]
        self.client.post(reverse("pet_report_edit", args=[report.pk]), {
            "report_type": "lost", "species": "dog", "description": "brown dog", "region": "dushanbe",
            "image": jpeg_with_gps(size=(800, 600)),
        })
        report.refresh_from_db()
        self.assert_clean_with_variants(report)
        self.assertNotEqual(report.image_variants["thumb"], old_thumb)
        self.assertFalse(report.image.storage.exists(old_thumb))  # stale variant files removed

    def test_admin_upload_is_stripped(self):
        admin = Users.objects.create_superuser(username="m6_admin", email="m6a@example.com", password=PASSWORD)
        self.client.force_login(admin)
        response = self.client.post(reverse("admin:myapp_photoreport_add"), {
            "title": "Акция", "description": "", "region": "", "image": jpeg_with_gps(), "image_variants": "{}",
            "author": admin.pk,
        })
        self.assertEqual(response.status_code, 302, response.context and response.context["adminform"].form.errors)
        self.assert_clean_with_variants(PhotoReport.objects.get())


@override_settings(MEDIA_ROOT=_MEDIA)
class VariantServiceTests(TestCase):
    def setUp(self):
        self.vol = Users.objects.create_user(username="m6_svc", email="m6s@example.com", password=PASSWORD,
                                             is_volunteer=True)
        self.profile = Profile.objects.get_or_create(user=self.vol)[0]

    def put(self, raw_upload):
        self.profile.image.save(os.path.basename(raw_upload.name), ContentFile(raw_upload.read()), save=True)

    def test_backfill_is_idempotent_and_strips_on_request(self):
        self.put(jpeg_with_gps(name="legacy.jpg"))  # pre-milestone-6 upload: GPS, no variants
        name = self.profile.image.name
        out = io.StringIO()
        call_command("backfill_image_variants", "--dry-run", "--strip-metadata", stdout=out)
        self.assertIn("accounts.Profile: would build variants for 1, would strip metadata from 1", out.getvalue())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.image_variants, {})
        call_command("backfill_image_variants", "--strip-metadata", stdout=io.StringIO())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.image.name, name)  # same path — URLs keep working
        self.assertNotIn(GPS_TAG, stored_exif(self.profile.image)[0])
        self.assertTrue(images.variants_current(self.profile.image, self.profile.image_variants))
        out = io.StringIO()
        call_command("backfill_image_variants", stdout=out)
        self.assertIn("total: variants 0, stripped 0, failed 0", out.getvalue())

    def test_removed_image_clears_variants(self):
        self.put(jpeg_with_gps(name="a.jpg"))
        images.apply_variants("accounts.Profile", self.profile.pk)
        self.profile.refresh_from_db()
        thumb = self.profile.image_variants["thumb"]
        Profile.objects.filter(pk=self.profile.pk).update(image="")
        self.profile.refresh_from_db()
        self.assertTrue(images.queue_variants(self.profile))  # eager without a broker
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.image_variants, {})
        self.assertFalse(self.profile.image.storage.exists(thumb))

    def test_unknown_model_refused(self):
        with self.assertRaises(ValueError):
            images.apply_variants("accounts.Users", 1)

    def test_tag_markup(self):
        self.put(jpeg_with_gps(name="b.jpg"))
        tpl = Template('{% load responsive %}{% responsive_image p.image p.image_variants variant="thumb" class="avatar" alt=name %}'
                       '|{% responsive_image p.image p.image_variants sizes="50vw" class="report-img" %}')
        before = tpl.render(Context({"p": self.profile, "name": "A & B"}))  # user data is escaped
        self.assertNotIn("<picture>", before)  # no variants yet → just the original
        self.assertIn('alt="A &amp; B"', before)
        images.apply_variants("accounts.Profile", self.profile.pk)
        self.profile.refresh_from_db()
        thumb, medium = (self.profile.image.storage.url(self.profile.image_variants[k]) for k in ("thumb", "medium"))
        single, multi = tpl.render(Context({"p": self.profile, "name": "A & B"})).split("|")
        self.assertEqual(single, f'<picture><source type="image/webp" srcset="{thumb}">'
                                 f'<img src="{self.profile.image.url}" alt="A &amp; B" class="avatar"></picture>')
        self.assertIn(f'srcset="{thumb} 320w, {medium} 960w" sizes="50vw"', multi)
        empty = Template('{% load responsive %}[{% responsive_image p.image p.image_variants %}]').render(
            Context({"p": Profile(user=self.vol)}))
        self.assertEqual(empty, "[]")


@override_settings(MEDIA_ROOT=_MEDIA)
class VariantDecodeTests(TestCase):
    """The worker decodes JPEGs at reduced scale (draft) and one at a time —
    the variants must still come out at full target size."""

    def test_large_jpeg_variants_keep_target_size(self):
        vol = Users.objects.create_user(username="m6_big", email="m6b@example.com", password=PASSWORD,
                                        is_volunteer=True)
        profile = Profile.objects.get_or_create(user=vol)[0]
        for size, expected in (((4032, 3024), (960, 720)), ((3024, 4032), (720, 960))):
            buf = io.BytesIO()
            Image.new("RGB", size, (90, 120, 150)).save(buf, "JPEG", quality=85)
            profile.image.save("big.jpg", ContentFile(buf.getvalue()), save=True)
            variants = images.make_webp_variants(profile.image)
            with profile.image.storage.open(variants["medium"]) as fh, Image.open(fh) as medium:
                self.assertEqual(medium.size, expected)
            with profile.image.storage.open(variants["thumb"]) as fh, Image.open(fh) as thumb:
                self.assertEqual(max(thumb.size), 320)

    def test_decode_lock_is_released_after_a_broken_file(self):
        vol = Users.objects.create_user(username="m6_broken", email="m6x@example.com", password=PASSWORD)
        profile = Profile.objects.get_or_create(user=vol)[0]
        profile.image.save("broken.jpg", ContentFile(b"not a jpeg"), save=True)
        with self.assertRaises(Exception):
            images.make_webp_variants(profile.image)
        self.assertTrue(images._DECODE_LOCK.acquire(blocking=False))  # not left held
        images._DECODE_LOCK.release()


def jpeg_bytes(size, exif=None):
    buf = io.BytesIO()
    kwargs = {"exif": exif} if exif is not None else {}
    Image.new("RGB", size, (90, 120, 150)).save(buf, "JPEG", quality=80, **kwargs)
    return buf.getvalue()


def opened(content):
    content.seek(0)
    return Image.open(io.BytesIO(content.read()))


@override_settings(MEDIA_ROOT=_MEDIA, TELEGRAM_BOT_TOKEN="")
class OriginalDownscaleTests(TestCase):
    """Owner decision 2026-10-04: stored uploads are at most 2560 px on the
    long side (never upscaled); the backfill's in-place strip keeps the size."""

    def test_long_side_capped_after_orientation(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # stored landscape, shown rotated 90° → portrait
        exif[GPS_TAG] = {1: "N", 2: (38.0, 33.0, 36.0)}
        upload = SimpleUploadedFile("p.jpg", jpeg_bytes((4032, 3024), exif), content_type="image/jpeg")
        clean, err = images.clean_original(upload)
        self.assertIsNone(err)
        with opened(clean) as img:
            self.assertEqual(img.size, (1920, 2560))
            self.assertNotIn(GPS_TAG, img.getexif())

    def test_small_photo_keeps_its_size(self):
        clean, _ = images.clean_original(jpeg_with_gps(size=(1600, 1200)))
        with opened(clean) as img:
            self.assertEqual(img.size, (1600, 1200))
        exact = SimpleUploadedFile("e.jpg", jpeg_bytes((2560, 1000)), content_type="image/jpeg")
        with opened(images.clean_original(exact)[0]) as img:
            self.assertEqual(img.size, (2560, 1000))

    def test_palette_png_is_resampled_not_nearest_neighbour(self):
        src = Image.new("P", (3000, 1000), 0)
        src.putpalette([0, 0, 0, 255, 255, 255] + [0] * 762)
        src.paste(1, (0, 0, 1500, 1000))
        src.info["transparency"] = 0
        buf = io.BytesIO()
        src.save(buf, "PNG", transparency=0)
        clean, err = images.clean_original(SimpleUploadedFile("t.png", buf.getvalue(), content_type="image/png"))
        self.assertIsNone(err)
        self.assertTrue(clean.name.endswith(".png"))
        with opened(clean) as img:
            self.assertEqual(img.size, (2560, 853))
            self.assertEqual(img.mode, "RGBA")
            self.assertEqual(img.getpixel((0, 0))[3], 255)       # opaque half kept
            self.assertEqual(img.getpixel((2559, 852))[3], 0)    # transparent half kept

    def test_huge_jpeg_is_decoded_at_reduced_scale(self):
        from PIL.JpegImagePlugin import JpegImageFile

        decoded = []
        original_draft = JpegImageFile.draft

        def spy(self, mode, size):
            result = original_draft(self, mode, size)
            decoded.append(self.size)
            return result

        upload = SimpleUploadedFile("big.jpg", jpeg_bytes((5400, 5200)), content_type="image/jpeg")
        with mock.patch.object(JpegImageFile, "draft", spy):
            clean, err = images.clean_original(upload)
        self.assertIsNone(err)
        self.assertIn((2700, 2600), decoded)  # 1/2 scale: memory ÷ 4, still ≥ 2560
        with opened(clean) as img:
            self.assertEqual(img.size, (2560, 2465))

    def test_chat_photo_is_capped_too(self):
        cache.clear()
        client = Users.objects.create_user(username="m6_cc", email="m6cc@example.com", password=PASSWORD,
                                           region="dushanbe", is_client=True)
        vol = Users.objects.create_user(username="m6_cv", email="m6cv@example.com", password=PASSWORD,
                                        region="dushanbe", is_volunteer=True)
        task = HelpRequest.objects.create(client=client, volunteer=vol, status="active", help_type="grocery",
                                          description="d", address="a", phone="+992901234567", region="dushanbe")
        upload = SimpleUploadedFile("c.jpg", jpeg_bytes((3200, 1800)), content_type="image/jpeg")
        outcome = chat.send_message(vol, task, image=upload)
        self.assertTrue(outcome.ok, outcome)
        stored = ChatMessage.objects.get(task=task).image
        with stored.storage.open(stored.name) as fh, Image.open(fh) as img:
            self.assertEqual(img.size, (2560, 1440))

    def test_backfill_strip_keeps_the_stored_size(self):
        vol = Users.objects.create_user(username="m6_legacy", email="m6l@example.com", password=PASSWORD)
        profile = Profile.objects.get_or_create(user=vol)[0]
        exif = Image.Exif()
        exif[GPS_TAG] = {1: "N", 2: (38.0, 33.0, 36.0)}
        profile.image.save("legacy.jpg", ContentFile(jpeg_bytes((3000, 2000), exif)), save=True)
        self.assertTrue(images.strip_stored_original(profile.image))
        with profile.image.storage.open(profile.image.name) as fh, Image.open(fh) as img:
            self.assertEqual(img.size, (3000, 2000))
            self.assertNotIn(GPS_TAG, img.getexif())
