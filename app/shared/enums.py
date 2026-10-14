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


class PayflowClientAddedThrough(str, Enum):
    ADD_CLIENT = "add_client"
    FILE_UPLOAD = "file_upload"


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
    FILE = "file"


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


class PayflowReviewPriority(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    NORMAL = "Normal"


class PayflowReviewStatus(str, Enum):
    AWAITING_REVIEW = "Awaiting Review"
    APPROVED = "Approved"
    MODIFIED = "Modified"
    REJECTED = "Rejected"
    ON_HOLD = "On Hold"
    COMPLETED = "Completed"


class PayflowRuleType(str, Enum):
    SYSTEM = "System Rule"
    CLIENT = "Client Rule"


class PayflowRuleStatus(str, Enum):
    DRAFT = "Draft"
    ACTIVE = "Active"
    INACTIVE = "Inactive"


class PayflowRuleLogic(str, Enum):
    ALL = "ALL"
    ANY = "ANY"


class PayflowStrategyStatus(str, Enum):
    AI_PROPOSED = "AI Proposed"
    DRAFT = "Draft"
    UNDER_REVIEW = "Under Review"
    APPROVED = "Approved"
    ACTIVE = "Active"
    INACTIVE = "Inactive"


class PayflowStrategyOrigin(str, Enum):
    AI_PROPOSED = "AI Proposed"
    HUMAN_CREATED = "Human Created"
    HUMAN_MODIFIED = "Human Modified"


class PayflowStrategySource(str, Enum):
    AI_GENERATED = "AI Generated"
    HUMAN_CREATED = "Human Created"


class PayflowCommChannel(str, Enum):
    EMAIL = "Email"
    SMS = "SMS"
    WHATSAPP = "WhatsApp"


class PayflowCommStatus(str, Enum):
    PREPARED = "Prepared"
    AWAITING_GOVERNANCE = "Awaiting Governance"
    APPROVED = "Approved"
    SCHEDULED = "Scheduled"
    SENT = "Sent"
    DELIVERED = "Delivered"
    OPENED_READ = "Opened / Read"
    PAYMENT_LINK_CLICKED = "Payment Link Clicked"
    FAILED = "Failed"
    SUPPRESSED = "Suppressed"


class PayflowCommPurpose(str, Enum):
    PAYMENT_REMINDER = "Payment Reminder"
    PROMISE_FOLLOW_UP = "Promise-to-Pay Follow-Up"
    PAYMENT_PLAN_REMINDER = "Payment Plan Reminder"
    SETTLEMENT_OFFER = "Settlement Offer"
    FINAL_NOTICE = "Final Notice"


class PayflowNotificationType(str, Enum):
    HUMAN_REVIEW_AWAITING = "human_review_awaiting"
    WORKFLOW_AWAITING_APPROVAL = "workflow_awaiting_approval"
    COMM_AWAITING_GOVERNANCE = "comm_awaiting_governance"
    INTEGRATION_CONNECTION_FAILED = "integration_connection_failed"
    SUPERVISOR_ASSIGNMENT = "supervisor_assignment"
    CLIENT_ACTIVATED = "client_activated"
    REVIEW_DECIDED = "review_decided"
    WORKFLOW_DECIDED = "workflow_decided"


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
