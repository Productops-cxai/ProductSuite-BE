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


class PayflowRoleCode(str, Enum):
    OPERATIONS_ADMIN = "operations_admin"
    SUPERVISOR = "supervisor"


class PayflowRoleScope(str, Enum):
    PLATFORM_WIDE = "platform_wide"
    CLIENT_SCOPED = "client_scoped"


class PayflowClientStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"


class PayflowClientType(str, Enum):
    FIRST_PARTY = "first_party"
    THIRD_PARTY = "third_party"


class PayflowBusinessDomain(str, Enum):
    COLLECTIONS = "collections"


class PayflowAiMode(str, Enum):
    AUTOPILOT = "autopilot"
    SUPERVISED_AI = "supervised_ai"


class PayflowDataSourceType(str, Enum):
    CRM = "crm"


class PayflowConnectionStatus(str, Enum):
    NOT_CONNECTED = "not_connected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    CONNECTION_FAILED = "connection_failed"


class PayflowMappingStatus(str, Enum):
    MAPPED = "mapped"
    NEEDS_ATTENTION = "needs_attention"
    UNMAPPED = "unmapped"
    VALIDATED = "validated"


class PayflowPortfolioStatus(str, Enum):
    ONBOARDING = "onboarding"
    ACTIVE = "active"
    PAUSED = "paused"


class PayflowCollectionStatus(str, Enum):
    """Display labels match Lovable Accounts / Cases UI."""

    ACTIVE = "Active"
    PROMISE_TO_PAY = "Promise to Pay"
    PAYMENT_PLAN = "Payment Plan"
    HUMAN_REVIEW = "Human Review"
    RESOLVED = "Resolved"


class PayflowIntegrationCategory(str, Enum):
    DATA_SOURCE = "Data Source"
    COMMUNICATION = "Communication"
    PAYMENTS = "Payments"
    FUTURE = "Future"


class PayflowIntegrationStatus(str, Enum):
    CONNECTED = "Connected"
    ATTENTION_REQUIRED = "Attention Required"
    DISCONNECTED = "Disconnected"
    CONFIGURATION_PENDING = "Configuration Pending"
    TESTING = "Testing"
    COMING_LATER = "Coming Later"


class PayflowOnboardingStepStatus(str, Enum):
    COMPLETE = "complete"
    PENDING = "pending"
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"


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
