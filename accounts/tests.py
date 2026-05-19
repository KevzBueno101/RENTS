import shutil
import tempfile
from pathlib import Path
from decimal import Decimal
from datetime import date

from django.contrib.auth.models import User
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
