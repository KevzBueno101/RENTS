import shutil
import tempfile
import re
from pathlib import Path
from decimal import Decimal
from datetime import date

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from django.test import TestCase, override_settings

from billing.services.receipt_generator import generate_receipt_for_payment
from tenant.services.dashboard_service import get_tenant_dashboard_data
from .models import Bill, Payment, Room, TenantProfile


TEST_MEDIA_ROOT = tempfile.mkdtemp(dir=Path("C:/tmp"))


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class ReceiptGenerationTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def test_generate_receipt_image_creates_expected_file_structure(self):
        user = User.objects.create_user(username="tenant1", password="testpass")
        room = Room.objects.create(
            room_number="101",
            floor=1,
            capacity=1,
            monthly_rate=Decimal("5000.00"),
        )
        tenant = TenantProfile.objects.create(
            user=user,
            full_name="Jane Tenant",
            phone="09170000000",
            room=room,
            room_number=room.room_number,
        )
        bill = Bill.objects.create(
            tenant=tenant,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            due_date=date(2026, 5, 5),
            total_amount=Decimal("5000.00"),
            status="sent",
        )
        payment = Payment.objects.create(
            bill=bill,
            amount=Decimal("5000.00"),
            payment_date=date(2026, 5, 2),
            payment_method="cash",
        )

        result = generate_receipt_for_payment(payment)
        payment.refresh_from_db()

        self.assertTrue(Path(result.absolute_path).exists())
        self.assertTrue(payment.receipt_image.name.startswith(f"receipts/tenant_{tenant.id}/receipt_"))
        self.assertTrue(payment.receipt_image.name.endswith(".png"))
        self.assertEqual(payment.receipt_id, result.receipt_id)


class TenantDashboardTests(TestCase):
    def setUp(self):
        self.tenant_user = User.objects.create_user(username="tenant_dash", password="testpass")
        self.other_user = User.objects.create_user(username="other_tenant", password="testpass")
        self.room = Room.objects.create(
            room_number="201",
            floor=2,
            capacity=2,
            monthly_rate=Decimal("4500.00"),
        )
        self.other_room = Room.objects.create(
            room_number="202",
            floor=2,
            capacity=2,
            monthly_rate=Decimal("4800.00"),
        )
        self.tenant = TenantProfile.objects.create(
            user=self.tenant_user,
            full_name="Tenant Dashboard",
            phone="09170000001",
            room=self.room,
            room_number=self.room.room_number,
        )
        self.other_tenant = TenantProfile.objects.create(
            user=self.other_user,
            full_name="Other Tenant",
            phone="09170000002",
            room=self.other_room,
            room_number=self.other_room.room_number,
        )

    def test_dashboard_loads_for_tenant(self):
        Bill.objects.create(
            tenant=self.tenant,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            due_date=date(2026, 5, 5),
            total_amount=Decimal("4500.00"),
            status="sent",
        )

        self.client.login(username="tenant_dash", password="testpass")
        response = self.client.get(reverse("tenant_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tenant/tenant_dashboard.html")
        self.assertIn("tenant", response.context)
        self.assertContains(response, "Current Balance")

    def test_dashboard_service_filters_to_current_tenant(self):
        own_bill = Bill.objects.create(
            tenant=self.tenant,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            due_date=date(2026, 5, 5),
            total_amount=Decimal("4500.00"),
            status="sent",
        )
        other_bill = Bill.objects.create(
            tenant=self.other_tenant,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            due_date=date(2026, 5, 5),
            total_amount=Decimal("9900.00"),
            status="sent",
        )
        Payment.objects.create(
            bill=own_bill,
            amount=Decimal("1000.00"),
            payment_date=date(2026, 5, 2),
            payment_method="cash",
        )
        Payment.objects.create(
            bill=other_bill,
            amount=Decimal("9900.00"),
            payment_date=date(2026, 5, 2),
            payment_method="cash",
        )

        data = get_tenant_dashboard_data(self.tenant_user)

        self.assertEqual(data["tenant"], self.tenant)
        self.assertEqual(data["summary"]["total_billed"], Decimal("4500.00"))
        self.assertEqual(data["summary"]["total_paid"], Decimal("1000.00"))
        self.assertEqual(data["balance"], Decimal("3500.00"))

    def test_dashboard_empty_state_without_payments(self):
        self.client.login(username="tenant_dash", password="testpass")
        response = self.client.get(reverse("tenant_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No payments recorded yet.")
        self.assertContains(response, "No receipt is available yet.")


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    SITE_URL="https://example.test",
    PASSWORD_RESET_EMAIL_RATE_LIMIT=20,
    PASSWORD_RESET_IP_RATE_LIMIT=50,
)
class PasswordResetFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        mail.outbox = []
        self.user = User.objects.create_user(
            username="reset_tenant",
            email="tenant@example.test",
            password="OldPass123!",
        )

    def _request_reset(self, email):
        return self.client.post(
            reverse("password_reset"),
            {"email": email},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

    def _reset_link_path(self):
        self.assertEqual(len(mail.outbox), 1)
        match = re.search(r"https://example\.test(?P<path>/password-reset/confirm/[^\s<]+)", mail.outbox[0].body)
        self.assertIsNotNone(match)
        return match.group("path")

    def test_reset_request_uses_generic_response_and_sends_email_for_existing_user(self):
        response = self._request_reset("tenant@example.test")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIn("If an account with that email exists", response.json()["message"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("https://example.test/password-reset/confirm/", mail.outbox[0].body)

    def test_reset_request_does_not_enumerate_unknown_email(self):
        response = self._request_reset("missing@example.test")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(len(mail.outbox), 0)

    def test_reset_confirm_updates_password_and_prevents_token_reuse(self):
        self._request_reset("tenant@example.test")
        reset_path = self._reset_link_path()

        response = self.client.post(
            reset_path,
            {"new_password1": "NewPass123!", "new_password2": "NewPass123!"},
        )

        self.assertRedirects(response, reverse("password_reset_complete"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewPass123!"))
        self.assertFalse(self.user.check_password("OldPass123!"))

        reused_response = self.client.get(reset_path)
        self.assertEqual(reused_response.status_code, 200)
        self.assertContains(reused_response, "Invalid Reset Link")

    def test_malformed_reset_link_is_handled_safely(self):
        response = self.client.get(
            reverse(
                "password_reset_confirm",
                kwargs={"uidb64": "not-a-valid-user", "token": "bad-token"},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid Reset Link")
