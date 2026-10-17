"""URLconf for myapp/tests_error_pages.py only: a view that raises, so the real
500 handler renders templates/500.html."""

from django.urls import path


def boom(request):
    raise RuntimeError("test: a deliberate server error")


urlpatterns = [path("boom/", boom)]
