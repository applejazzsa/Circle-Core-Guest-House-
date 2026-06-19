from django.conf import settings
from django.contrib.auth import login as auth_login
from django.shortcuts import redirect, render, resolve_url
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.generic import View

from .forms import EmailLoginForm, PhonePinLoginForm


@method_decorator(sensitive_post_parameters("password", "pin"), name="dispatch")
@method_decorator(csrf_protect, name="dispatch")
@method_decorator(never_cache, name="dispatch")
class DualLoginView(View):
    template_name = "login.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(self.get_success_url(request))
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self, request):
        redirect_to = request.POST.get("next") or request.GET.get("next")
        if redirect_to and url_has_allowed_host_and_scheme(
            redirect_to,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect_to
        return resolve_url(settings.LOGIN_REDIRECT_URL)

    def get(self, request):
        return self.render_forms(request, login_method=request.GET.get("method", "email"))

    def post(self, request):
        login_method = request.POST.get("login_method", "email")
        email_form = EmailLoginForm(request=request, data=request.POST if login_method == "email" else None)
        pin_form = PhonePinLoginForm(request=request, data=request.POST if login_method == "pin" else None)
        active_form = pin_form if login_method == "pin" else email_form

        if active_form.is_valid():
            auth_login(request, active_form.get_user())
            return redirect(self.get_success_url(request))

        return render(
            request,
            self.template_name,
            {
                "email_form": email_form,
                "pin_form": pin_form,
                "login_method": login_method,
                "next": request.POST.get("next", ""),
                "login_failed": True,
            },
            status=200,
        )

    def render_forms(self, request, login_method="email"):
        if login_method not in ("email", "pin"):
            login_method = "email"
        return render(
            request,
            self.template_name,
            {
                "email_form": EmailLoginForm(request=request),
                "pin_form": PhonePinLoginForm(request=request),
                "login_method": login_method,
                "next": request.GET.get("next", ""),
            },
        )
