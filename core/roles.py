OWNER_ROLE = "Owner"
MANAGER_ROLE = "Manager"
RECEPTION_ROLE = "Reception"
OPERATOR_ROLE = "Operator"
CLEANER_ROLE = "Cleaner"
VIEWER_ROLE = "Viewer"

# Operator is retained for existing tenants. Manager is its clearer replacement.
STAFF_ROLES = [
    OWNER_ROLE,
    MANAGER_ROLE,
    RECEPTION_ROLE,
    CLEANER_ROLE,
    VIEWER_ROLE,
    OPERATOR_ROLE,
]


def user_has_role(user, role_name):
    if not user.is_authenticated:
        return False
    if user.is_superuser and role_name == OWNER_ROLE:
        return True
    return user.groups.filter(name=role_name).exists()


def is_owner(user):
    return user_has_role(user, OWNER_ROLE)


def is_operator(user):
    return user_has_role(user, OPERATOR_ROLE) or user_has_role(user, MANAGER_ROLE)


def is_manager(user):
    return user_has_role(user, MANAGER_ROLE) or user_has_role(user, OPERATOR_ROLE)


def is_reception(user):
    return user_has_role(user, RECEPTION_ROLE)


def is_cleaner(user):
    return user_has_role(user, CLEANER_ROLE)


def is_viewer(user):
    return user_has_role(user, VIEWER_ROLE)


def primary_role(user):
    if is_owner(user):
        return OWNER_ROLE
    if user_has_role(user, MANAGER_ROLE):
        return MANAGER_ROLE
    if is_reception(user):
        return RECEPTION_ROLE
    if user_has_role(user, OPERATOR_ROLE):
        return OPERATOR_ROLE
    if is_cleaner(user):
        return CLEANER_ROLE
    if is_viewer(user):
        return VIEWER_ROLE
    if user.is_staff:
        return OPERATOR_ROLE
    return "Staff"


def can_manage_business(user):
    return is_owner(user) or is_manager(user) or is_reception(user) or user.is_staff


def can_manage_system(user):
    return is_owner(user)


def assign_role(user, role_name):
    if role_name not in STAFF_ROLES:
        raise ValueError("Unknown staff role.")

    from django.contrib.auth.models import Group
    from .models import StaffProfile

    user.groups.remove(*Group.objects.filter(name__in=STAFF_ROLES))
    group, _ = Group.objects.get_or_create(name=role_name)
    user.groups.add(group)
    profile, _ = StaffProfile.objects.get_or_create(user=user)
    if profile.role != role_name:
        profile.role = role_name
        profile.save(update_fields=["role", "updated_at"])
