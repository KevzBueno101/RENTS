"""
Authentication-related views: login, signup, logout, profile management.
"""
import logging

from django.shortcuts import render, redirect
from django.urls import reverse
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.utils.http import url_has_allowed_host_and_scheme
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.views import PasswordResetView
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.tokens import default_token_generator
from django.http import HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.contrib import messages
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.utils.crypto import salted_hmac
from django.template.loader import render_to_string
from ..models import Room, TenantProfile, AdminProfile
from ..activity_utils import log_activity
from .helpers import parse_phone, get_available_rooms, get_dashboard_context
from accounts.services.user_service import UserService
from accounts.forms import ProfileUpdateForm

from accounts.rbac.admin_login_throttle import (
    clear_admin_attempts,
    get_client_ip,
    is_admin_login_locked,
    record_admin_failed_login,
)
from accounts.rbac.policy import SESSION_PORTAL_KEY


logger = logging.getLogger(__name__)
PASSWORD_RESET_GENERIC_MESSAGE = (
    'If an account with that email exists, a password reset link has been sent.'
)


def _safe_next_redirect(request, fallback_route: str):
    next_candidate = request.GET.get('next') or request.POST.get('next')
    if next_candidate and url_has_allowed_host_and_scheme(
        next_candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_candidate)
    return redirect(fallback_route)


def _tenant_login_context(**extra):
    context = {
        'available_rooms': get_available_rooms(),
        'active_tenants': TenantProfile.objects.filter(user__is_active=True).count(),
    }
    context.update(extra)
    return context


def tenant_login_view(request):
    """Tenant portal sign-in — staff accounts cannot authenticate here."""

    if request.method == 'GET' and request.user.is_authenticated and request.user.is_staff:
        return redirect('admin_dashboard')

    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')

        user = authenticate(request, username=username, password=password)

        if user is not None and getattr(user, 'is_staff', False):
            return render(request, 'login.html', _tenant_login_context(
                error_modal=True,
                error='Invalid username or password.',
            ))

        if user is not None:
            request.session.cycle_key()
            login(request, user)
            request.session[SESSION_PORTAL_KEY] = 'tenant'
            return _safe_next_redirect(request, 'tenant_dashboard')

        return render(request, 'login.html', _tenant_login_context(
            error_modal=True,
            error='Invalid username or password.',
        ))

    return render(request, 'login.html', _tenant_login_context())


def admin_login_view(request):
    """Dedicated administrator portal login (staff users only)."""
    ip = get_client_ip(request)

    if is_admin_login_locked(ip):
        return HttpResponseForbidden('Too many failed attempts. Please try again later.')

    if request.method == 'GET' and request.user.is_authenticated and not request.user.is_staff:
        return redirect('tenant_dashboard')

    if request.method == 'GET' and request.user.is_authenticated and request.user.is_staff:
        return _safe_next_redirect(request, 'admin_dashboard')

    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')

        user = authenticate(request, username=username, password=password)

        if user is not None and getattr(user, 'is_staff', False):
            clear_admin_attempts(ip)
            request.session.cycle_key()
            login(request, user)
            request.session[SESSION_PORTAL_KEY] = 'admin'
            return _safe_next_redirect(request, 'admin_dashboard')

        record_admin_failed_login(ip)
        msg = (
            'Invalid username or password.'
            if user is None
            else 'This account cannot use the administrator sign-in.'
        )
        return render(request, 'admin_login.html', {
            'error_modal': True,
            'error': msg,
        })

    return render(request, 'admin_login.html')


login_view = tenant_login_view


def admin_locked_placeholder(request):  # noqa: ARG001
    raise NotImplementedError


def signup_view(request):
    """Handle tenant self-signup with room selection."""
    if request.method == 'POST':
        username  = request.POST.get('username')
        password  = request.POST.get('password')
        email     = request.POST.get('email')
        full_name = request.POST.get('full_name')
        phone_raw = request.POST.get('phone')
        phone     = parse_phone(phone_raw)
        
        # Validate phone number
        if phone_raw and not phone:
            return render(request, 'login.html', _tenant_login_context(
                signup_error='Phone number must contain 10-15 digits only.',
            ))
        
        room_id   = request.POST.get('room_id')
        photo     = request.FILES.get('photo')

        if not room_id:
            return render(request, 'login.html', _tenant_login_context(
                signup_error='Please select a room.',
            ))

        if User.objects.filter(username=username).exists():
            return render(request, 'login.html', _tenant_login_context(
                signup_error='Username already taken. Please choose another.',
            ))

        if User.objects.filter(email__iexact=email).exists():
            return render(request, 'login.html', _tenant_login_context(
                signup_error='This email is already registered. Please use a different email.',
            ))

        try:
            selected_room = Room.objects.get(id=room_id)
        except Room.DoesNotExist:
            return render(request, 'login.html', _tenant_login_context(
                signup_error='Selected room no longer available.',
            ))

        user = User.objects.create_user(
            username=username,
            password=password,
            email=email
        )

        TenantProfile.objects.create(
            user=user,
            full_name=full_name,
            phone=phone,
            room=selected_room,
            room_number=selected_room.room_number,
            photo=photo
        )

        return render(request, 'login.html', _tenant_login_context(
            signup_success='Account created! You can now log in as Tenant.',
        ))

    return redirect('login')


def logout_view(request):
    """Handle user logout."""
    portal = request.session.pop(SESSION_PORTAL_KEY, None)
    logout(request)
    if portal == 'admin':
        return redirect(reverse('admin_login'))
    return redirect(reverse('login'))


@login_required(login_url='/login/')
def edit_profile(request):
    """Role-aware profile page/update endpoint for admins and tenants."""
    if request.user.is_staff:
        return _edit_admin_profile(request)
    return _edit_tenant_profile(request)


def _edit_admin_profile(request):
    if request.method == 'POST':
        username         = request.POST.get('username')
        full_name        = request.POST.get('full_name')
        email            = request.POST.get('email')
        phone            = parse_phone(request.POST.get('phone'))
        current_password = request.POST.get('current_password')
        new_password     = request.POST.get('new_password') or request.POST.get('password')
        confirm_password = request.POST.get('confirm_password')
        photo            = request.FILES.get('photo')
        print("DEBUG _edit_admin_profile: POST =", request.POST)
        print("DEBUG _edit_admin_profile: FILES =", request.FILES)
        print("DEBUG _edit_admin_profile: photo =", photo)

        errors = []

        if username and User.objects.filter(username=username).exclude(id=request.user.id).exists():
            errors.append('Username is already taken.')

        if email and User.objects.filter(email__iexact=email).exclude(id=request.user.id).exists():
            errors.append('Email is already registered.')

        if current_password and not request.user.check_password(current_password):
            errors.append('Current password is incorrect.')

        if new_password and new_password != confirm_password:
            errors.append('New passwords do not match.')

        if errors:
            return render(request, 'admin/dashboard.html', {
                **get_dashboard_context(),
                'profile_errors'    : errors,
                'show_profile_modal': True,
            })

        user          = request.user
        user.username = username or user.username
        user.email    = email or ''
        if new_password:
            user.set_password(new_password)
        user.save()
        if new_password:
            update_session_auth_hash(request, user)

        try:
            admin_profile = AdminProfile.objects.get(user=user)
        except AdminProfile.DoesNotExist:
            admin_profile = AdminProfile(user=user, created_by=user, full_name=full_name or user.username, phone=phone or '')

        admin_profile.full_name = full_name or user.username
        admin_profile.phone     = phone or ''
        if photo:
            admin_profile.photo = photo
        admin_profile.save()
        messages.success(request, 'Profile updated successfully.')
        return redirect('admin_dashboard')

    return redirect('admin_dashboard')


def _edit_tenant_profile(request):
    try:
        profile = TenantProfile.objects.select_related('user', 'room').get(user=request.user)
    except TenantProfile.DoesNotExist:
        messages.error(request, 'Tenant profile not found.')
        return redirect('tenant_dashboard')

    form = ProfileUpdateForm(request.POST or None, instance=request.user)

    if request.method == 'POST':
        full_name        = request.POST.get('full_name', '').strip()
        current_password = request.POST.get('current_password')
        new_password     = request.POST.get('new_password') or request.POST.get('password')
        confirm_password = request.POST.get('confirm_password')
        photo            = request.FILES.get('photo')
        print("DEBUG _edit_tenant_profile: POST =", request.POST)
        print("DEBUG _edit_tenant_profile: FILES =", request.FILES)
        print("DEBUG _edit_tenant_profile: photo =", photo)

        errors = []

        # Validate form (username, email, phone)
        if not form.is_valid():
            errors.extend([err for field_errors in form.errors.values() for err in field_errors])

        if not full_name or len(full_name) < 2:
            errors.append('Full name must be at least 2 characters long.')

        if new_password:
            if not current_password:
                errors.append('Current password is required to change your password.')
            elif not request.user.check_password(current_password):
                errors.append('Current password is incorrect.')
            elif len(new_password) < 8:
                errors.append('New password must be at least 8 characters long.')
            elif new_password != confirm_password:
                errors.append('New passwords do not match.')

        if errors:
            return render(request, 'tenant/tenant_profile.html', {
                'profile': profile,
                'form': form,
                'profile_errors': errors,
            })

        # Save user fields (username, email)
        user = form.save(commit=False)
        if new_password:
            user.set_password(new_password)
        user.save()
        if new_password:
            update_session_auth_hash(request, user)

        # Save tenant profile fields
        profile.full_name = full_name
        # Get validated phone from form
        if 'phone' in form.cleaned_data:
            profile.phone = form.cleaned_data['phone']
        if photo:
            profile.photo = photo
        profile.save()

        log_activity(
            user=request.user,
            action='tenant_updated',
            description='Updated profile information',
            content_type='TenantProfile',
            object_id=profile.id
        )

        messages.success(request, 'Profile updated successfully.')
        return redirect('edit_profile')

    completion_fields = {
        'full_name': bool(profile.full_name),
        'email': bool(profile.user.email),
        'phone': bool(profile.phone),
        'photo': bool(profile.photo),
        'room': bool(profile.room)
    }
    completion_percentage = (sum(completion_fields.values()) / len(completion_fields)) * 100

    return render(request, 'tenant/tenant_profile.html', {
        'profile': profile,
        'form': ProfileUpdateForm(instance=request.user),
        'completion_percentage': completion_percentage,
        'completion_fields': completion_fields,
        'filled_fields_count': sum(completion_fields.values()),
    })


class CustomPasswordResetForm(PasswordResetForm):
    """PasswordResetForm whose send_mail is actually invoked by form.save(); tracks delivery failures."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mail_delivery_failed = False

    def save(
        self,
        domain_override=None,
        subject_template_name='registration/password_reset_subject.txt',
        email_template_name='registration/password_reset_email.html',
        use_https=False,
        token_generator=default_token_generator,
        from_email=None,
        request=None,
        html_email_template_name=None,
        extra_email_context=None,
    ):
        self.mail_delivery_failed = False
        return super().save(
            domain_override=domain_override,
            subject_template_name=subject_template_name,
            email_template_name=email_template_name,
            use_https=use_https,
            token_generator=token_generator,
            from_email=from_email,
            request=request,
            html_email_template_name=html_email_template_name,
            extra_email_context=extra_email_context,
        )

    def send_mail(
        self,
        subject_template_name,
        email_template_name,
        context,
        from_email,
        to_email,
        html_email_template_name=None,
    ):
        """Send mail with SendGrid API, SMTP fallback, console fallback for DEBUG; marks mail_delivery_failed."""
        from django.conf import settings
        from django.core.mail import send_mail as django_send_mail

        # Try SendGrid HTTP API first when configured (not SMTP)
        try:
            sg_key = getattr(settings, 'SENDGRID_API_KEY', '') or ''
            if sg_key:
                import sendgrid
                from sendgrid.helpers.mail import Mail

                subject = render_to_string(subject_template_name, context)
                subject = ''.join(subject.splitlines())

                message = Mail(
                    from_email=from_email,
                    to_emails=to_email,
                    subject=subject,
                    html_content=(
                        render_to_string(html_email_template_name, context) if html_email_template_name else None
                    ),
                    plain_text_content=render_to_string(email_template_name, context),
                )

                sg = sendgrid.SendGridAPIClient(sg_key)
                response = sg.send(message)

                logger.info('SendGrid API email sent: %s to %s', response.status_code, to_email)
                return

        except Exception:
            logger.exception('SendGrid API failed for %s', to_email)
            # In production, don't block the request with SMTP fallbacks that may hang
            # (e.g. outbound SMTP restrictions) — fail fast and let the view return 503/JSON.
            if not settings.DEBUG:
                self.mail_delivery_failed = True
                return

        try:
            return PasswordResetForm.send_mail(
                self,
                subject_template_name,
                email_template_name,
                context,
                from_email,
                to_email,
                html_email_template_name=html_email_template_name,
            )

        except Exception as smtp_error:
            logger.error('SMTP send failed for %s: %s', to_email, smtp_error, exc_info=True)

            try:
                from django.core.mail import get_connection

                connection = get_connection('django.core.mail.backends.console.EmailBackend')
                subject = render_to_string(subject_template_name, context)
                subject = ''.join(subject.splitlines())

                if html_email_template_name:
                    html_body = render_to_string(html_email_template_name, context)
                    email_message = EmailMultiAlternatives(subject, '', from_email, [to_email], connection=connection)
                    email_message.attach_alternative(html_body, 'text/html')
                    email_message.send()
                else:
                    plain_text = render_to_string(email_template_name, context)
                    django_send_mail(subject, plain_text, from_email, [to_email], connection=connection)

                logger.info('Console fallback email sent to %s', to_email)

            except Exception as console_error:
                logger.error('Console email fallback failed for %s: %s', to_email, console_error)
                logger.warning('All email methods failed for %s', to_email)
                self.mail_delivery_failed = True


class CustomPasswordResetView(PasswordResetView):
    """Password reset: SITE_URL in emails (domain_override), JSON for AJAX from login modal."""

    template_name = 'registration/password_reset_form.html'
    email_template_name = 'registration/password_reset_email.txt'
    html_email_template_name = 'registration/password_reset_email.html'
    subject_template_name = 'registration/password_reset_subject.txt'
    success_url = '/password-reset/done/'
    form_class = CustomPasswordResetForm

    @staticmethod
    def _success_payload():
        return {
            'ok': True,
            'email_sent': True,
            'message': PASSWORD_RESET_GENERIC_MESSAGE,
        }

    @staticmethod
    def _expects_json(request):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return True
        accept = (request.headers.get('Accept') or '').lower()
        return 'application/json' in accept

    def form_invalid(self, form):
        if self._expects_json(self.request):
            return JsonResponse(
                {'ok': False, 'email_sent': False, 'errors': form.errors.as_json()},
                status=400,
            )
        return super().form_invalid(form)

    def _generic_success_response(self):
        if self._expects_json(self.request):
            return JsonResponse(self._success_payload())

        messages.info(self.request, PASSWORD_RESET_GENERIC_MESSAGE)
        return HttpResponseRedirect(self.get_success_url())

    def _is_rate_limited(self, email):
        from django.conf import settings

        window = int(getattr(settings, 'PASSWORD_RESET_RATE_WINDOW', 900))
        email_limit = int(getattr(settings, 'PASSWORD_RESET_EMAIL_RATE_LIMIT', 5))
        ip_limit = int(getattr(settings, 'PASSWORD_RESET_IP_RATE_LIMIT', 20))
        ip = get_client_ip(self.request)
        email_normalized = (email or '').strip().lower()

        def over_limit(scope, raw_value, limit):
            if limit <= 0:
                return False
            digest = salted_hmac(
                'password-reset-rate-limit',
                f'{scope}:{raw_value}',
            ).hexdigest()
            key = f'pwd-reset:{scope}:{digest}'
            if cache.add(key, 1, timeout=window):
                return False
            try:
                attempts = cache.incr(key)
            except ValueError:
                cache.set(key, 1, timeout=window)
                return False
            return attempts > limit

        return (
            over_limit('ip', ip, ip_limit)
            or over_limit('email', email_normalized, email_limit)
        )

    def form_valid(self, form):
        from django.conf import settings

        site_url = (getattr(settings, 'SITE_URL', '') or '').strip().rstrip('/')
        domain_only = (
            site_url.replace('http://', '', 1).replace('https://', '', 1)
            if site_url
            else ''
        )
        use_https = site_url.startswith('https://')

        extra = {
            'site_name': getattr(settings, 'SITE_NAME', 'RENTS System'),
            'password_reset_timeout_hours': max(
                1,
                int(getattr(settings, 'PASSWORD_RESET_TIMEOUT', 86400)) // 3600,
            ),
        }
        if self.extra_email_context:
            extra.update(self.extra_email_context)

        email = form.cleaned_data.get('email', '')
        if self._is_rate_limited(email):
            logger.warning('Password reset request rate-limited for IP %s', get_client_ip(self.request))
            return self._generic_success_response()

        opts = {
            'use_https': use_https,
            'domain_override': domain_only or None,
            'token_generator': self.token_generator,
            'from_email': (
                self.from_email
                or getattr(settings, 'DEFAULT_FROM_EMAIL', None)
                or getattr(settings, 'FROM_EMAIL', None)
            ),
            'email_template_name': self.email_template_name,
            'subject_template_name': self.subject_template_name,
            'request': self.request,
            'html_email_template_name': self.html_email_template_name,
            'extra_email_context': extra,
        }

        try:
            form.save(**opts)
        except Exception as exc:
            logger.exception('Password reset email dispatch failed.')
            return self._generic_success_response()

        failed = getattr(form, 'mail_delivery_failed', False)
        if failed:
            logger.error('Password reset email delivery failed.')
            return self._generic_success_response()

        if self._expects_json(self.request):
            return JsonResponse(self._success_payload())

        return self._generic_success_response()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from django.conf import settings

        su = (getattr(settings, 'SITE_URL', '') or '').strip().rstrip('/')
        if su:
            context['domain'] = su.replace('http://', '').replace('https://', '')
            context['protocol'] = 'https' if su.startswith('https://') else 'http'
        return context

def custom_password_reset_confirm(request, uidb64, token):
    """
    Simple password reset confirm view that bypasses Django's authentication middleware.
    """
    from django.contrib.auth import get_user_model
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.http import urlsafe_base64_decode
    from django.contrib.auth.forms import SetPasswordForm
    from django.shortcuts import render, redirect
    from django.contrib import messages
    
    User = get_user_model()
    
    # Try to decode the user ID
    try:
        uid = urlsafe_base64_decode(uidb64).decode()
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None
    
    # Check if the token is valid
    validlink = user is not None and default_token_generator.check_token(user, token)
    
    if request.method == 'POST':
        if validlink:
            form = SetPasswordForm(user, request.POST)
            if form.is_valid():
                form.save()
                messages.success(request, 'Your password has been reset successfully. You can now log in with your new password.')
                return redirect('password_reset_complete')
        else:
            # Invalid token - show error
            return render(request, 'registration/password_reset_confirm.html', {
                'validlink': False,
                'form': None
            })
    else:
        if validlink:
            form = SetPasswordForm(user)
        else:
            form = None
    
    return render(request, 'registration/password_reset_confirm.html', {
        'validlink': validlink,
        'form': form,
        'user': user
    })
