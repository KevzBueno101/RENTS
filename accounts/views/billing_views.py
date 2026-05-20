"""
Billing and payment views: list, generate, edit, view, delete, record payment.
"""
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Q, Sum
from django.utils.http import url_has_allowed_host_and_scheme
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
import os
import logging
from datetime import datetime
from ..models import Bill, Notification, Payment, TenantProfile
from ..activity_utils import log_activity, get_recent_activities
from ..services.notification_service import NotificationService
from notifications.services import DynamicNotificationService
from billing.services.receipt_generator import (
    generate_receipt_for_payment,
    send_receipt_to_tenant,
)

logger = logging.getLogger(__name__)


def _can_access_payment(user, payment):
    if user.is_staff:
        return True
    return payment.bill.tenant.user_id == user.id


def _get_accessible_payment_or_404(request, payment_id):
    payment = get_object_or_404(
        Payment.objects.select_related('bill', 'bill__tenant', 'bill__tenant__user', 'bill__room'),
        id=payment_id,
    )
    if not _can_access_payment(request.user, payment):
        raise Http404("Receipt not found")
    return payment


def _redirect_back(request, fallback='billing_list'):
    next_url = request.META.get('HTTP_REFERER')
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect(fallback)


@login_required(login_url='/admin/login/')
def billing_list(request):
    """List all bills with filtering and statistics."""
    if not request.user.is_staff:
        return redirect('tenant_dashboard')


    search = request.GET.get('search', '')
    status_filter = request.GET.get('status', '')
    sort_by = request.GET.get('sort', '-created_at')

    bills = Bill.objects.select_related('tenant', 'tenant__user', 'room').prefetch_related('payments')

    if search:
        bills = bills.filter(
            Q(bill_number__icontains=search) |
            Q(tenant__full_name__icontains=search) |
            Q(tenant__user__username__icontains=search)
        )

    if status_filter:
        bills = bills.filter(status=status_filter)

    bills = bills.order_by(sort_by)

    # Get all tenants for the generate bill modal
    all_tenants = TenantProfile.objects.select_related('user', 'room').filter(room__isnull=False).order_by('full_name')

    # Statistics
    stats = {
        'total_bills': bills.count(),
        'outstanding': bills.filter(status__in=['sent', 'overdue']).aggregate(
            total=Sum('total_amount')
        )['total'] or 0,
        'overdue': bills.filter(status='overdue').aggregate(
            total=Sum('total_amount')
        )['total'] or 0,
        'paid': bills.filter(status='paid').aggregate(
            total=Sum('total_amount')
        )['total'] or 0,
        'partial': bills.filter(status='partial').count(),
        'draft': bills.filter(status='draft').count(),
    }

    return render(request, 'admin/billing_list.html', {
        'bills': bills,
        'stats': stats,
        'search': search,
        'status_filter': status_filter,
        'sort_by': sort_by,
        'all_tenants': all_tenants,
        'activities': get_recent_activities(limit=10),
    })


@login_required(login_url='/admin/login/')
def generate_bill(request):
    """Generate a new bill."""
    if not request.user.is_staff:
        return redirect('tenant_dashboard')

    if request.method == 'POST':
        tenant_id = request.POST.get('tenant')
        period_start = request.POST.get('period_start')
        period_end = request.POST.get('period_end')
        due_date = request.POST.get('due_date')
        total_amount = request.POST.get('total_amount')
        notes = request.POST.get('notes', '')
        save_as_draft = request.POST.get('save_as_draft') == 'on'

        # Autofill rules (server-side safety):
        # - Period End = Period Start + 1 month
        # - Due Date = Period End + 5 days
        # - Total Amount = Room monthly_rate (if missing/invalid)
        from datetime import timedelta

        def _add_months(d, months):
            # Clamp overflow dates to last day of target month.
            month = d.month - 1 + months
            year = d.year + month // 12
            month = month % 12 + 1
            # last day of target month:
            import calendar
            last_day = calendar.monthrange(year, month)[1]
            day = min(d.day, last_day)
            return datetime(year, month, day).date()

        parsed_period_start = None
        parsed_period_end = None
        parsed_due_date = None
        try:
            parsed_period_start = datetime.strptime(period_start, '%Y-%m-%d').date() if period_start else None
        except Exception:
            parsed_period_start = None

        # If we have period_start, recompute period_end + due_date regardless of what came from UI.
        if parsed_period_start:
            parsed_period_end = _add_months(parsed_period_start, 1)
            parsed_due_date = parsed_period_end + timedelta(days=5)
        else:
            # Fallback to posted values
            try:
                parsed_period_end = datetime.strptime(period_end, '%Y-%m-%d').date() if period_end else None
            except Exception:
                parsed_period_end = None
            try:
                parsed_due_date = datetime.strptime(due_date, '%Y-%m-%d').date() if due_date else None
            except Exception:
                parsed_due_date = None

        # Total amount: if missing/0/invalid, use room monthly_rate from tenant.room.
        parsed_total_amount = None
        try:
            if total_amount is not None and str(total_amount).strip() != '':
                parsed_total_amount = float(total_amount)
        except Exception:
            parsed_total_amount = None

        # We'll set final total_amount after we load tenant and access room.monthly_rate.

        try:
            tenant = TenantProfile.objects.get(id=tenant_id)
        except TenantProfile.DoesNotExist:
            messages.error(request, 'Tenant not found')
            return redirect('billing_list')

        from datetime import datetime

        # Apply computed autofill values (if available)
        final_period_start = parsed_period_start
        final_period_end = parsed_period_end
        final_due_date = parsed_due_date

        # Reuse posted period_start if parsed_period_start is None
        if final_period_start is None and period_start:
            try:
                final_period_start = datetime.strptime(period_start, '%Y-%m-%d').date()
            except Exception:
                final_period_start = None

        # Total amount: use room monthly_rate if missing/invalid, otherwise use parsed_total_amount.
        final_total_amount = total_amount
        try:
            room_rate = tenant.room.monthly_rate if tenant.room else None
        except Exception:
            room_rate = None

        if parsed_total_amount is None and room_rate is not None:
            final_total_amount = room_rate
        elif parsed_total_amount is not None:
            final_total_amount = parsed_total_amount

        bill = Bill(
            tenant=tenant,
            period_start=final_period_start,
            period_end=final_period_end,
            due_date=final_due_date,
            total_amount=final_total_amount,
            notes=notes,
            status='draft' if save_as_draft else 'sent'
        )
        bill.save()

        log_activity(
            user=request.user,
            action='bill_generated',
            description=f'Generated bill {bill.bill_number} for {tenant.full_name} (₱{total_amount})',
            content_type='Bill',
            object_id=bill.id
        )

        # Create notification for tenant about new bill (only if not draft)
        if not save_as_draft:
            try:
                DynamicNotificationService.create(
                    user=tenant.user,
                    type_code='billing',
                    context={
                        'title': 'New Bill Generated',
                        'message': f'A new bill {bill.bill_number} has been generated for you with total amount of ₱{bill.total_amount:,.2f}. Due date: {bill.due_date}.',
                        'bill_number': bill.bill_number,
                        'amount': bill.total_amount,
                        'due_date': bill.due_date
                    },
                    link='/tenant/billing/'
                )
            except Exception as e:
                # Log error but don't fail the bill creation
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to create billing notification: {e}")

        messages.success(request, f'Bill {bill.bill_number} generated successfully!')
    return redirect('billing_list')


@login_required(login_url='/admin/login/')
def edit_bill(request, bill_id):
    """Edit an existing bill."""
    if not request.user.is_staff:
        return redirect('tenant_dashboard')

    try:
        bill = Bill.objects.get(id=bill_id)
    except Bill.DoesNotExist:
        messages.error(request, 'Bill not found')
        return redirect('billing_list')

    if request.method == 'POST':
        if bill.status == 'paid':
            messages.error(request, 'Cannot edit paid bills')
            return redirect('billing_list')

        from datetime import datetime

        period_start = request.POST.get('period_start')
        period_end = request.POST.get('period_end')
        due_date = request.POST.get('due_date')

        bill.period_start = datetime.strptime(period_start, '%Y-%m-%d').date() if period_start else None
        bill.period_end = datetime.strptime(period_end, '%Y-%m-%d').date() if period_end else None
        bill.due_date = datetime.strptime(due_date, '%Y-%m-%d').date() if due_date else None
        bill.total_amount = request.POST.get('total_amount')
        bill.notes = request.POST.get('notes', '')
        bill.status = request.POST.get('status', bill.status)
        bill.save()

        log_activity(
            user=request.user,
            action='bill_updated',
            description=f'Updated bill {bill.bill_number} for {bill.tenant.full_name}',
            content_type='Bill',
            object_id=bill.id
        )

        messages.success(request, f'Bill {bill.bill_number} updated successfully!')
        return redirect('view_bill', bill_id=bill.id)

    return redirect('billing_list')


@login_required(login_url='/admin/login/')
def view_bill(request, bill_id):
    """View bill details."""
    if not request.user.is_staff:
        return redirect('tenant_dashboard')

    try:
        bill = Bill.objects.select_related('tenant', 'tenant__user', 'room').prefetch_related('payments', 'items').get(id=bill_id)
    except Bill.DoesNotExist:
        return redirect('billing_list')

    return render(request, 'admin/billing_view.html', {'bill': bill})


@login_required(login_url='/admin/login/')
def delete_bill(request, bill_id):
    """Delete a bill."""
    if not request.user.is_staff:
        return redirect('tenant_dashboard')

    if request.method == 'POST':
        try:
            bill = Bill.objects.get(id=bill_id)
            bill_number = bill.bill_number
            tenant_name = bill.tenant.full_name
            bill.delete()
            
            log_activity(
                user=request.user,
                action='bill_deleted',
                description=f'Deleted bill {bill_number} for {tenant_name}',
                content_type='Bill',
                object_id=bill_id
            )
            
            messages.success(request, f'Bill {bill_number} deleted successfully!')
        except Bill.DoesNotExist:
            messages.error(request, 'Bill not found')

    return redirect('billing_list')


@login_required(login_url='/admin/login/')
def record_payment(request, bill_id):
    """Record a payment for a bill."""
    if not request.user.is_staff:
        return redirect('tenant_dashboard')

    try:
        bill = Bill.objects.get(id=bill_id)
    except Bill.DoesNotExist:
        messages.error(request, 'Bill not found')
        return redirect('billing_list')

    if request.method == 'POST':
        amount = request.POST.get('amount')
        payment_date = request.POST.get('payment_date')
        payment_method = request.POST.get('payment_method')
        reference_number = request.POST.get('reference_number', '')
        notes = request.POST.get('notes', '')
        proof = request.FILES.get('proof')

        from datetime import datetime

        payment = Payment.objects.create(
            bill=bill,
            amount=amount,
            payment_date=datetime.strptime(payment_date, '%Y-%m-%d').date() if payment_date else None,
            payment_method=payment_method,
            reference_number=reference_number,
            notes=notes,
            proof=proof
        )

        bill.update_status()

        try:
            generate_receipt_for_payment(payment)
        except Exception:
            messages.warning(request, 'Payment was recorded, but the receipt image could not be generated.')

        log_activity(
            user=request.user,
            action='payment_recorded',
            description=f'Recorded payment of ₱{amount} from {bill.tenant.full_name}',
            content_type='Payment',
            object_id=payment.id
        )

        # Create notification for tenant about payment
        try:
            DynamicNotificationService.create(
                user=bill.tenant.user,
                type_code='payment',
                context={
                    'title': 'Payment Recorded',
                    'message': f'Your payment of ₱{float(amount):,.2f} for bill {bill.bill_number} has been recorded successfully.',
                    'amount': float(amount),
                    'bill_number': bill.bill_number
                },
                link='/tenant/billing/'
            )
        except Exception as e:
            # Log error but don't fail the payment process
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to create payment notification: {e}")

        messages.success(request, f'Payment of ₱{amount} recorded successfully!')
        return redirect('billing_list')

    return redirect('billing_list')


@login_required(login_url='/admin/login/')
def generate_payment_receipt(request, payment_id):
    """Generate or regenerate a PNG receipt for a payment."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    payment = _get_accessible_payment_or_404(request, payment_id)
    if not request.user.is_staff:
        return JsonResponse({'error': 'Only staff can generate receipts'}, status=403)

    result = generate_receipt_for_payment(payment)
    return JsonResponse({
        'receipt_url': payment.receipt_image.url,
        'receipt_id': result.receipt_id,
        'download': True,
    })

@login_required(login_url='/login/')
def download_payment_receipt(request, payment_id):
    """Download a generated PNG receipt."""
    payment = _get_accessible_payment_or_404(request, payment_id)
    if not payment.receipt_image:
        raise Http404("Receipt has not been generated")

    import requests as http_requests

    # Get the Cloudinary URL
    try:
        receipt_url = payment.receipt_image.url
    except Exception:
        raise Http404("Receipt file not found")

    # Fetch from Cloudinary and serve as download
    try:
        response = http_requests.get(receipt_url, timeout=15)
        response.raise_for_status()
    except Exception:
        raise Http404("Receipt file could not be retrieved")

    filename = f"{payment.receipt_id or 'receipt'}.png"
    http_response = HttpResponse(response.content, content_type='image/png')
    http_response['Content-Disposition'] = f'attachment; filename="{filename}"'
    http_response['Content-Length'] = len(response.content)
    return http_response

@login_required(login_url='/admin/login/')
def send_payment_receipt(request, payment_id):
    """Send a generated receipt notification to the tenant dashboard."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    payment = _get_accessible_payment_or_404(request, payment_id)
    if not request.user.is_staff:
        return JsonResponse({'error': 'Only staff can send receipts'}, status=403)

    if not payment.receipt_image:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Receipt has not been generated'}, status=400)
        messages.error(request, 'Receipt has not been generated yet.')
        return _redirect_back(request)

    result = send_receipt_to_tenant(payment.receipt_image.name, payment.bill.tenant)
    DynamicNotificationService.create(
        user=payment.bill.tenant.user,
        type_code='payment',
        context={
            'title': 'Payment receipt available',
            'message': (
                f'Your payment receipt for {payment.bill.bill_number} '
                f'(amount: ₱{payment.amount:,.2f}) is now available for download.'
            )
        },
        link=f'/billing/payments/{payment.id}/download-receipt/'
    )

    log_activity(
        user=request.user,
        action='reminder_sent',
        description=f'Sent receipt notification for {payment.bill.bill_number} to {payment.bill.tenant.full_name}',
        content_type='Payment',
        object_id=payment.id
    )

    result = {**result, 'queued': True, 'channel': 'in_app'}
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse(result)

    messages.success(request, f'Receipt notification sent to {payment.bill.tenant.full_name}.')
    return _redirect_back(request)


@login_required(login_url='/admin/login/')
def delete_payment(request, payment_id):
    """Delete a payment."""
    if not request.user.is_staff:
        return redirect('tenant_dashboard')

    try:
        payment = Payment.objects.get(id=payment_id)
        bill = payment.bill
        amount = payment.amount
        payment.delete()
        bill.update_status()
        
        log_activity(
            user=request.user,
            action='payment_deleted',
            description=f'Deleted payment of ₱{amount} from {bill.tenant.full_name}',
            content_type='Payment',
            object_id=payment_id
        )
        
        return redirect('view_bill', bill_id=bill.id)
    except Payment.DoesNotExist:
        messages.error(request, 'Payment not found')
        return redirect('billing_list')


@login_required(login_url='/admin/login/')
def mark_as_sent(request, bill_id):
    """Mark a bill as sent."""
    if not request.user.is_staff:
        return redirect('tenant_dashboard')

    try:
        bill = Bill.objects.get(id=bill_id)
        bill.status = 'sent'
        bill.save(update_fields=['status'])
        
        log_activity(
            user=request.user,
            action='bill_sent',
            description=f'Marked bill {bill.bill_number} as sent to {bill.tenant.full_name}',
            content_type='Bill',
            object_id=bill.id
        )
        
        messages.success(request, f'Bill {bill.bill_number} marked as sent!')
    except Bill.DoesNotExist:
        messages.error(request, 'Bill not found')

    return redirect('billing_list')


@login_required(login_url='/login/')
def upload_payment_proof(request):
    """
    Handle payment proof upload from tenant Contact Admin modal.
    Creates a new Payment record with the uploaded screenshot.
    """
    if request.method == 'POST':
        bill_id = request.POST.get('bill_id')
        payment_proof = request.FILES.get('payment_proof')
        notes = request.POST.get('notes', '')
        amount = request.POST.get('amount', '0')
        
        # Validate bill exists and belongs to current tenant
        if not bill_id:
            return JsonResponse({'success': False, 'error': 'No active bill found to pay.'})
            
        try:
            bill_id = int(bill_id)
        except (ValueError, TypeError):
            return JsonResponse({'success': False, 'error': 'Invalid bill reference.'})
            
        try:
            bill = Bill.objects.get(id=bill_id, tenant__user=request.user)
        except Bill.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Bill not found'})
        
        # Validate amount
        try:
            amount = float(amount)
            if amount <= 0:
                return JsonResponse({'success': False, 'error': 'Payment amount must be greater than 0'})
        except (ValueError, TypeError):
            return JsonResponse({'success': False, 'error': 'Invalid payment amount'})
        
        # Validate file
        if not payment_proof:
            return JsonResponse({'success': False, 'error': 'Please select a file'})
        
        # Validate file type
        allowed_types = ['image/jpeg', 'image/jpg', 'image/png', 'image/webp']
        if payment_proof.content_type not in allowed_types:
            return JsonResponse({'success': False, 'error': 'Invalid file type. Please upload JPG, PNG, or WebP'})
        
        # Validate file size (5MB max)
        if payment_proof.size > 5 * 1024 * 1024:
            return JsonResponse({'success': False, 'error': 'File size must be less than 5MB'})
        
        try:
            # Create Payment record with the provided amount
            payment = Payment.objects.create(
                bill=bill,
                amount=amount,
                payment_date=datetime.now().date(),
                payment_method='gcash',  # Default to GCash for tenant uploads
                reference_number=f'PROOF_{datetime.now().strftime("%Y%m%d_%H%M%S")}',
                proof=payment_proof,
                notes=notes,
            )
            
            # Update bill status based on payment
            bill.update_status()
            
            # Log activity
            log_activity(
                user=request.user,
                action='payment_proof_uploaded',
                description=f'Uploaded payment proof for bill {bill.bill_number}',
                content_type='Payment',
                object_id=payment.id
            )
            
            # Create notification for admin/staff users
            try:
                from django.contrib.auth.models import User
                staff_users = User.objects.filter(is_staff=True)
                for staff_user in staff_users:
                    DynamicNotificationService.create(
                        user=staff_user,
                        type_code='payment_proof',
                        context={
                            'title': 'Payment proof uploaded',
                            'message': f'{request.user.tenantprofile.full_name} uploaded payment proof for bill {bill.bill_number}'
                        },
                        link='/billing/'
                    )
            except Exception as e:
                logger.error(f"Failed to create admin notification: {e}")

            
            return JsonResponse({
                'success': True, 
                'message': 'Payment proof uploaded successfully! Admin will review and process your payment.',
                'payment_id': payment.id
            })
            
        except Exception as e:
            return JsonResponse({'success': False, 'error': f'Upload failed: {str(e)}'})
    
    return JsonResponse({'success': False, 'error': 'Invalid request method'})
