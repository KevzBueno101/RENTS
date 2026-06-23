# TODO: Profile Section Redesign (Facebook-style, dynamic backend)

- [ ] Inspect current URL routing + existing profile edit/update endpoints.
- [ ] Implement dynamic profile storage model (JSON-based) for tenant/admin.
- [ ] Add database migration for the new model.
- [ ] Implement API endpoints:
  - [ ] GET profile (with role-based visibility)
  - [ ] PATCH picture
  - [ ] PATCH about
  - [ ] PATCH contact
  - [ ] PATCH professional
  - [ ] PUT batch update
- [ ] Add caching + invalidation for profile GET.
- [ ] Add/extend audit log when profile sections change.
- [ ] Update frontend template `accounts/templates/tenant/tenant_profile.html`:
  - [ ] Remove old two-column card layout
  - [ ] Add Facebook-style tabs
  - [ ] Add editable fields inside tab panels (view/edit mode)
  - [ ] Keep NO cover photo
- [ ] Add frontend JS to call new APIs (load profile, save sections, show loading/success).
- [ ] Add/adjust CSS for responsive cards + tab transitions.
- [ ] Wire new endpoints in `accounts/urls.py` and/or `accounts/urlconf/routes.py`.
- [ ] Run migrations and perform manual QA (desktop + mobile).

