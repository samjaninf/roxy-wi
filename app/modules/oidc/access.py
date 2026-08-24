from typing import Optional

import app.modules.roxywi.common as roxywi_common
from app.modules.subscription.access import (
    FEATURE_POLICIES,
    OIDC,
    feature_required,
    is_feature_available,
)


OIDC_ALLOWED_PLANS = FEATURE_POLICIES[OIDC].allowed_plans
OIDC_SUBSCRIPTION_ERROR = FEATURE_POLICIES[OIDC].error


def is_oidc_available(subscription: Optional[dict] = None) -> bool:
    return is_feature_available(OIDC, subscription)


oidc_subscription_required = feature_required(OIDC)
