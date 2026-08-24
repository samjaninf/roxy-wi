from typing import Optional

import app.modules.roxywi.common as roxywi_common
from app.modules.subscription.access import (
    CHANGE_CENTER,
    FEATURE_POLICIES,
    feature_required,
    is_feature_available,
)


CHANGE_CENTER_PLAN = next(iter(FEATURE_POLICIES[CHANGE_CENTER].allowed_plans))
CHANGE_CENTER_SUBSCRIPTION_ERROR = FEATURE_POLICIES[CHANGE_CENTER].error


def is_change_center_available(subscription: Optional[dict] = None) -> bool:
    return is_feature_available(CHANGE_CENTER, subscription)


change_center_subscription_required = feature_required(CHANGE_CENTER)
