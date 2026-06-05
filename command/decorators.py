from functools import wraps

from django.shortcuts import redirect


def command_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.session.get("command_user_id"):
            return redirect("/command/login/")
        return view_func(request, *args, **kwargs)
    return wrapper
