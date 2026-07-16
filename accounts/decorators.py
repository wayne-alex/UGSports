from functools import wraps
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def ward_admin_required(view_func):
    @wraps(view_func)
    @login_required(login_url='login_admin')
    def wrapper(request, *args, **kwargs):
        if request.user.role != request.user.Role.WARD_ADMIN:
            raise PermissionDenied("This area is restricted to Ward Admins.")
        if not request.user.ward_id:
            raise PermissionDenied("Your account has no ward assigned — contact your County ICT Officer.")
        return view_func(request, *args, **kwargs)
    return wrapper

def subcounty_admin_required(view_func):
    @wraps(view_func)
    @login_required(login_url='login_admin')
    def wrapper(request, *args, **kwargs):
        if request.user.role != request.user.Role.SUB_COUNTY_ADMIN:
            raise PermissionDenied("This area is restricted to Sub-County Admins.")
        if not request.user.sub_county_id:
            raise PermissionDenied("Your account has no sub-county assigned — contact your County ICT Officer.")
        return view_func(request, *args, **kwargs)
    return wrapper


def superadmin_admin_required(view_func):
    @wraps(view_func)
    @login_required(login_url='login_admin')
    def wrapper(request, *args, **kwargs):
        # Create a clean list of allowed roles
        allowed_roles = [
            request.user.Role.SYSTEM_ADMIN,
            request.user.Role.COUNTY_ICT_OFFICER
        ]

        # If the user's role is not one of these, deny access
        if request.user.role not in allowed_roles:
            raise PermissionDenied("This area is restricted to System Admin or COUNTY_ICT_OFFICER.")

        return view_func(request, *args, **kwargs)

    return wrapper