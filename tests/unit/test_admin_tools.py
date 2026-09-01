import pytest

from app.routes.admin import routes as admin_routes


def test_tools_route_leaves_failures_to_the_global_error_handler(app, monkeypatch):
    monkeypatch.setattr(admin_routes.roxywi_auth, 'page_for_admin', lambda: None)
    monkeypatch.setattr(
        admin_routes.roxywi_common,
        'get_user_lang_for_flask',
        lambda: 'en',
    )
    monkeypatch.setattr(
        admin_routes.tools_common,
        'get_services_status',
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError('tools failed')),
    )

    with app.test_request_context('/admin/tools'):
        with pytest.raises(RuntimeError, match='tools failed'):
            admin_routes.show_tools()
