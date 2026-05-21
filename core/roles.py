OWNER_ROLE = "Owner"
OPERATOR_ROLE = "Operator"
CLEANER_ROLE = "Cleaner"
STAFF_ROLES = [OWNER_ROLE, OPERATOR_ROLE, CLEANER_ROLE]


def user_has_role(user, role_name):
    if not user.is_authenticated:
        return False
    if user.is_superuser and role_name == OWNER_ROLE:
        return True
    return user.groups.filter(name=role_name).exists()


def is_owner(user):
    return user_has_role(user, OWNER_ROLE)


def is_operator(user):
    return user_has_role(user, OPERATOR_ROLE)


def is_cleaner(user):
    return user_has_role(user, CLEANER_ROLE)


def primary_role(user):
    if is_owner(user):
        return OWNER_ROLE
    if is_operator(user):
        return OPERATOR_ROLE
    if is_cleaner(user):
        return CLEANER_ROLE
    if user.is_staff:
        return OPERATOR_ROLE
    return "Staff"


def can_manage_business(user):
    return is_owner(user) or is_operator(user) or user.is_staff


def can_manage_system(user):
    return is_owner(user)
