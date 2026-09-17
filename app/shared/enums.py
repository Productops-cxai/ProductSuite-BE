from enum import Enum


class ProductStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    INACTIVE = "inactive"


class EntitlementStatus(str, Enum):
    GRANTED = "granted"
    REVOKED = "revoked"


class UserStatus(str, Enum):
    INVITED = "invited"
    ACTIVE = "active"
    DISABLED = "disabled"


# Alias used by older call sites
PersonStatus = UserStatus


class PlatformRole(str, Enum):
    PLATFORM_SUPER_ADMIN = "platform_super_admin"
    PLATFORM_USER = "platform_user"


class AuthTokenType(str, Enum):
    ACTIVATION = "activation"
    PASSWORD_RESET = "password_reset"


class LoginNextStep(str, Enum):
    PLATFORM_ADMIN = "platform_admin"
    PRODUCT_SELECTION = "product_selection"
    DIRECT_ENTRY = "direct_entry"
    NO_ACCESS = "no_access"


class MenuContext(str, Enum):
    PLATFORM_ADMIN = "platform_admin"
    PRODUCT = "product"
