"""
Maintenance Service for Tenant Dashboard

Provides tenant-specific maintenance data for the dashboard.
Follows strict user filtering for security and privacy.
"""

from django.utils import timezone
from accounts.models import MaintenanceReport, TenantProfile, Bill, Notification


def get_tenant_maintenance_summary(user):
    """
    Get maintenance summary for a specific tenant
    
    Args:
        user: The authenticated user (tenant)
        
    Returns:
        dict: Maintenance data with counts and latest requests
    """
    try:
        tenant_profile = user.tenantprofile
    except TenantProfile.DoesNotExist:
        return {
            "total": 0,
            "open": 0,
            "in_progress": 0,
            "resolved": 0,
            "latest": []
        }
    
    # Get all maintenance requests for this tenant
    requests = MaintenanceReport.objects.filter(tenant=tenant_profile)
    
    return {
        "total": requests.count(),
        "open": requests.filter(status='open').count(),
        "in_progress": requests.filter(status='ongoing').count(),
        "resolved": requests.filter(status='completed').count(),
        "latest": requests.order_by('-created_at')[:3]
    }


def get_upcoming_payment(user):
    """
    Get the next upcoming payment for a tenant
    
    Args:
        user: The authenticated user (tenant)
        
    Returns:
        dict or None: Payment information or None if no pending payments
    """
    try:
        tenant_profile = user.tenantprofile
    except TenantProfile.DoesNotExist:
        return None
    
    # Get the next pending bill (not paid, not overdue)
    bill = Bill.objects.filter(
        tenant=tenant_profile,
        status__in=['sent', 'partial']
    ).order_by('due_date').first()
    
    if not bill:
        return None
    
    # Calculate days until due
    today = timezone.now().date()
    days_left = (bill.due_date - today).days if bill.due_date else 0
    
    # Business logic for overdue calculation and alert styling
    is_overdue = days_left < 0
    days_overdue = abs(days_left) if is_overdue else 0
    
    # Determine alert class based on days left
    if is_overdue:
        alert_class = "danger"
    elif days_left <= 3:
        alert_class = "danger"
    elif days_left <= 7:
        alert_class = "warning"
    else:
        alert_class = "success"
    
    # Status label for display
    if is_overdue:
        status_label = "Overdue"
    elif days_left == 0:
        status_label = "Due Today"
    else:
        status_label = "Upcoming"
    
    return {
        "amount": bill.total_amount,
        "due_date": bill.due_date,
        "days_left": days_left,
        "days_overdue": days_overdue,
        "is_overdue": is_overdue,
        "alert_class": alert_class,
        "status_label": status_label,
        "bill_number": bill.bill_number,
        "status": bill.status
    }


def get_activity_timeline(user):
    """
    Get recent activity timeline for a tenant
    
    Args:
        user: The authenticated user (tenant)
        
    Returns:
        QuerySet: Recent notifications/activities
    """
    # Get recent notifications for this tenant
    return Notification.objects.filter(
        user=user
    ).order_by('-created_at')[:5]


def get_dashboard_summary(user):
    """
    Get complete dashboard summary for a tenant
    
    Args:
        user: The authenticated user (tenant)
        
    Returns:
        dict: Complete dashboard data
    """
    return {
        "maintenance": get_tenant_maintenance_summary(user),
        "payment": get_upcoming_payment(user),
        "activities": get_activity_timeline(user)
    }
