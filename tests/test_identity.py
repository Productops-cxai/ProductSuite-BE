from app.modules.identity.service import resolve_next_step
from app.shared.enums import LoginNextStep, PlatformRole


class _User:
    def __init__(self, role_code):
        self.role_code = role_code


class _Product:
    def __init__(self, code):
        self.code = code


def test_next_step_super_admin():
    assert (
        resolve_next_step(_User(PlatformRole.PLATFORM_SUPER_ADMIN.value), [_Product("PAYFLOW")])
        == LoginNextStep.PLATFORM_ADMIN
    )


def test_next_step_single_product():
    assert (
        resolve_next_step(_User(PlatformRole.PLATFORM_USER.value), [_Product("PAYFLOW")])
        == LoginNextStep.DIRECT_ENTRY
    )


def test_next_step_multiple_products():
    assert (
        resolve_next_step(
            _User(PlatformRole.PLATFORM_USER.value),
            [_Product("PAYFLOW"), _Product("INSIGHTIQ")],
        )
        == LoginNextStep.PRODUCT_SELECTION
    )


def test_next_step_no_access():
    assert resolve_next_step(_User(PlatformRole.PLATFORM_USER.value), []) == LoginNextStep.NO_ACCESS
