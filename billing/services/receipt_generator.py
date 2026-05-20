import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from django.conf import settings
from django.utils import timezone

from billing.utils.image_renderer import render_receipt_png


@dataclass(frozen=True)
class ReceiptResult:
    receipt_id: str
    absolute_path: str
    relative_path: str


class BaseReceiptGenerator(ABC):
    format = None

    @abstractmethod
    def generate(self, payment):
        raise NotImplementedError


class PNGReceiptGenerator(BaseReceiptGenerator):
    format = "png"

    def generate(self, payment):
        import tempfile

        receipt_id = payment.receipt_id or f"receipt_{uuid.uuid4().hex}"
        relative_path = Path("receipts") / f"tenant_{payment.bill.tenant_id}" / f"{receipt_id}.png"

        # Write to a temp file instead of MEDIA_ROOT (Cloudinary has no local MEDIA_ROOT)
        tmp_dir = Path(tempfile.gettempdir()) / "rents_receipts"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        absolute_path = tmp_dir / f"{receipt_id}.png"

        data = build_receipt_data(payment, receipt_id)
        render_receipt_png(data, absolute_path)

        return ReceiptResult(
            receipt_id=receipt_id,
            absolute_path=str(absolute_path),
            relative_path=relative_path.as_posix(),
        )


class PDFReceiptGenerator(BaseReceiptGenerator):
    format = "pdf"

    def generate(self, payment):
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        receipt_id = payment.receipt_id or f"receipt_{uuid.uuid4().hex}"
        relative_path = Path("receipts") / f"tenant_{payment.bill.tenant_id}" / f"{receipt_id}.pdf"
        absolute_path = Path(settings.MEDIA_ROOT) / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)

        data = build_receipt_data(payment, receipt_id)
        pdf = canvas.Canvas(str(absolute_path), pagesize=A4)
        width, height = A4
        pdf.setFont("Helvetica-Bold", 20)
        pdf.drawString(72, height - 72, data["system_name"])
        pdf.setFont("Helvetica", 14)
        pdf.drawString(72, height - 100, "Official Payment Receipt")

        y = height - 150
        for label in ("receipt_id", "tenant_name", "room", "date", "billing_period", "payment_method", "status"):
            pdf.drawString(72, y, f"{label.replace('_', ' ').title()}: {data[label]}")
            y -= 24
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(72, y - 12, f"Amount Paid: {data['amount_display']}")
        pdf.save()

        return ReceiptResult(
            receipt_id=receipt_id,
            absolute_path=str(absolute_path),
            relative_path=relative_path.as_posix(),
        )


class ReceiptGenerator:
    generators = {
        PNGReceiptGenerator.format: PNGReceiptGenerator,
        PDFReceiptGenerator.format: PDFReceiptGenerator,
    }

    def __init__(self, output_format="png"):
        try:
            generator_class = self.generators[output_format]
        except KeyError as exc:
            raise ValueError(f"Unsupported receipt format: {output_format}") from exc
        self.generator = generator_class()

    def generate(self, payment):
        return self.generator.generate(payment)


def format_billing_period(bill):
    if bill.period_start and bill.period_end:
        start = bill.period_start.strftime("%b %d, %Y")
        end = bill.period_end.strftime("%b %d, %Y")
        return f"{start} - {end}"
    return bill.bill_number or "-"


def build_receipt_data(payment, receipt_id=None):
    bill = payment.bill
    tenant = bill.tenant
    amount = Decimal(payment.amount or 0)
    payment_date = payment.payment_date or timezone.localdate()

    return {
        "receipt_id": receipt_id or payment.receipt_id or f"receipt_{uuid.uuid4().hex}",
        "tenant_id": tenant.id,
        "tenant_name": tenant.full_name,
        "room": bill.room.room_code if bill.room else tenant.get_room_display(),
        "amount": float(amount),
        "amount_display": f"PHP {amount:,.2f}",
        "date": payment_date.strftime("%B %d, %Y"),
        "payment_method": payment.get_payment_method_display(),
        "billing_period": format_billing_period(bill),
        "status": "Paid",
        "system_name": "RENTS Boarding House Management System",
    }


def generate_receipt_image(data):
    """
    Generate a PNG receipt from a structured dict.

    Expected keys include tenant_id, receipt_id, tenant_name, room, amount, date,
    payment_method, and billing_period.
    """
    tenant_id = data["tenant_id"]
    receipt_id = data.get("receipt_id") or f"receipt_{uuid.uuid4().hex}"
    relative_path = Path("receipts") / f"tenant_{tenant_id}" / f"{receipt_id}.png"
    absolute_path = Path(settings.MEDIA_ROOT) / relative_path
    render_receipt_png({**data, "receipt_id": receipt_id, "status": data.get("status", "Paid")}, absolute_path)
    return str(absolute_path)



def generate_receipt_for_payment(payment, output_format="png", save=True):
    result = ReceiptGenerator(output_format).generate(payment)

    if save:
        payment.receipt_id = result.receipt_id

        if output_format == "png":
            # Upload the locally-generated file to Django's storage backend (Cloudinary)
            from django.core.files import File
            from django.core.files.storage import default_storage

            local_path = Path(result.absolute_path)
            storage_name = result.relative_path  # e.g. "receipts/tenant_15/receipt_xxx.png"

            # Delete existing file at that path in storage if it exists
            if default_storage.exists(storage_name):
                default_storage.delete(storage_name)

            # Upload via Django storage (this sends it to Cloudinary)
            with open(local_path, "rb") as f:
                saved_name = default_storage.save(storage_name, File(f))

            # Clean up local temp file
            try:
                local_path.unlink()
            except Exception:
                pass

            payment.receipt_image.name = saved_name

        payment.save(update_fields=["receipt_id", "receipt_image"])

    return result


def generate_receipts_for_payments(payments, output_format="png"):
    return [generate_receipt_for_payment(payment, output_format=output_format) for payment in payments]


def send_receipt_to_tenant(receipt_path, tenant):
    """
    Future dispatch integration point for email, SMS, and in-app notification delivery.
    """
    return {
        "queued": False,
        "tenant_id": tenant.id,
        "receipt_path": receipt_path,
        "channels": ["email", "sms", "in_app"],
        "message": "Receipt delivery is structured but not enabled yet.",
    }
