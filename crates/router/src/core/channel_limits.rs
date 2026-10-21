//! Shared payment guard for V1 and V2; connector implementations remain untouched.
use serde_json::{json, Value};

use crate::{
    core::errors::{ApiErrorResponse, RouterResult},
    routes::SessionState,
};

pub fn reserves(flow: &str) -> bool {
    !matches!(
        flow.rsplit("::").next().unwrap_or(flow),
        "PSync"
            | "RSync"
            | "Void"
            | "PostCaptureVoid"
            | "PostCaptureVoidSync"
            | "PreAuthorizeVoid"
            | "Reject"
            | "CaptureSync"
            | "Session"
            | "SessionToken"
            | "SdkSession"
            | "PaymentMethodToken"
            | "AccessTokenAuth"
            | "VerifyWebhookSource"
            | "PreAuthenticate"
            | "Authenticate"
            | "PostAuthenticate"
    )
}

pub async fn reserve(state: &SessionState, payload: Value) -> RouterResult<()> {
    if !state.conf.platform.channel_daily_limits_enabled {
        return Ok(());
    }
    let outcome = state
        .store
        .channel_limit_command("reserve", payload)
        .await?;
    if !reservation_allowed(
        &outcome,
        state.conf.platform.channel_daily_limits_require_binding,
    ) {
        return Err(ApiErrorResponse::PreconditionFailed {
            message: "Channel daily quota unavailable; payment was not submitted to this channel"
                .to_owned(),
        }
        .into());
    }
    Ok(())
}

fn reservation_allowed(outcome: &Value, require_binding: bool) -> bool {
    outcome.get("allowed").and_then(Value::as_bool) == Some(true)
        && (!require_binding || outcome.get("managed").and_then(Value::as_bool) == Some(true))
}

pub async fn settle(
    state: &SessionState,
    mut payload: Value,
    status: common_enums::AttemptStatus,
    http_status: Option<u16>,
    successful_response: bool,
) -> RouterResult<()> {
    if !state.conf.platform.channel_daily_limits_enabled {
        return Ok(());
    }
    let outcome = settlement_state(status, http_status, successful_response);
    if let Some(outcome) = outcome {
        payload["state"] = json!(outcome);
        state.store.channel_limit_command("settle", payload).await?;
    }
    Ok(())
}

fn settlement_state(
    status: common_enums::AttemptStatus,
    http_status: Option<u16>,
    successful_response: bool,
) -> Option<&'static str> {
    use common_enums::AttemptStatus as S;
    match status {
        S::Charged
        | S::PartialCharged
        | S::PartialChargedAndChargeable
        | S::AutoRefunded
        | S::VoidedPostCharge => Some("committed"),
        S::Voided if successful_response => Some("released"),
        S::Failure | S::AuthorizationFailed | S::AuthenticationFailed | S::RouterDeclined
            if successful_response
                || http_status.is_some_and(|n| {
                    (400..500).contains(&n) && !matches!(n, 408 | 409 | 425 | 429)
                }) =>
        {
            Some("released")
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unbound_channels_are_denied_when_binding_is_required() {
        let unbound = json!({"allowed": true, "managed": false});
        let bound = json!({"allowed": true, "managed": true});
        assert!(reservation_allowed(&unbound, false));
        assert!(!reservation_allowed(&unbound, true));
        assert!(reservation_allowed(&bound, true));
        assert!(!reservation_allowed(
            &json!({"allowed": false, "managed": true}),
            true
        ));
    }
    #[test]
    fn guard_charge_flows_and_skip_read_only_sync() {
        for flow in [
            "Authorize",
            "CompleteAuthorize",
            "Capture",
            "IncrementalAuthorization",
            "PreProcessing",
        ] {
            assert!(reserves(flow));
        }
        for flow in ["PSync", "Void", "RSync"] {
            assert!(!reserves(flow));
        }
    }
    #[test]
    fn ambiguous_results_hold_quota_and_refunds_keep_usage() {
        use common_enums::AttemptStatus as S;
        for status in [None, Some(408), Some(409), Some(425), Some(429), Some(500)] {
            assert_eq!(settlement_state(S::Failure, status, false), None);
        }
        assert_eq!(settlement_state(S::Authorized, Some(200), true), None);
        assert_eq!(
            settlement_state(S::Failure, Some(402), false),
            Some("released")
        );
        assert_eq!(
            settlement_state(S::AuthorizationFailed, Some(200), true),
            Some("released")
        );
        assert_eq!(
            settlement_state(S::Voided, Some(200), true),
            Some("released")
        );
        assert_eq!(
            settlement_state(S::AutoRefunded, Some(200), true),
            Some("committed")
        );
        assert_eq!(
            settlement_state(S::PartialCharged, Some(200), true),
            Some("committed")
        );
    }
}
