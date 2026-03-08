from functools import wraps
from django.shortcuts import redirect, render


def role_required(allowed_roles: list):
    """Restrict a view to users whose role is in allowed_roles.

    Unauthenticated users → redirect to /auth/login/.
    Wrong role → render 403 page (HTTP 403, not a redirect).
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect('/auth/login/')
            if request.user.role not in allowed_roles:
                return render(request, '403.html', status=403)
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator
