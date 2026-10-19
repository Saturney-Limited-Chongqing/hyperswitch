//! Service-key boundary for platform-managed collection merchants.
use std::collections::HashSet;

use common_utils::id_type;

use crate::{
    core::errors::{ApiErrorResponse, RouterResult},
    services::authentication::AuthenticationType,
};

/// Use exact flow names: new administrative APIs must not become accessible just
/// because they share a URL or name prefix with a collection API.
fn is_collection_flow(flow: &str) -> bool {
    matches!(
        flow,
        "PaymentsCreate"
            | "PaymentsRetrieve"
            | "PaymentsRetrieveForceSync"
            | "PaymentsRetrieveUsingMerchantReferenceId"
            | "PaymentsUpdate"
            | "PaymentsConfirm"
            | "PaymentsCapture"
            | "PaymentsCancel"
            | "PaymentsSessionToken"
            | "PaymentsStart"
            | "PaymentsList"
            | "PaymentsFilters"
            | "PaymentsAggregate"
            | "PaymentsCreateIntent"
            | "PaymentsGetIntent"
            | "PaymentsUpdateIntent"
            | "PaymentsConfirmIntent"
            | "PaymentsCreateAndConfirmIntent"
            | "PaymentAttemptsList"
            | "PaymentsRedirect"
            | "PaymentsCompleteAuthorize"
            | "RefundsCreate"
            | "RefundsRetrieve"
            | "RefundsRetrieveForceSync"
            | "RefundsList"
            | "RefundsFilters"
            | "RefundsAggregate"
            | "PaymentLinkCreate"
            | "PaymentLinkRetrieve"
            | "PaymentLinkInitiate"
            | "PaymentLinkList"
            | "PaymentLinkStatus"
            | "CustomersCreate"
            | "CustomersRetrieve"
            | "CustomersRetrieveByReferenceId"
            | "CustomersUpdate"
            | "CustomersDelete"
            | "CustomersList"
            | "CustomersListWithConstraints"
            | "PaymentMethodsCreate"
            | "PaymentMethodsRetrieve"
            | "PaymentMethodsUpdate"
            | "PaymentMethodsDelete"
            | "PaymentMethodsList"
            | "CustomerPaymentMethodsList"
    )
}

pub fn check_service_key_flow(
    collection_merchants: &HashSet<id_type::MerchantId>,
    auth: &AuthenticationType,
    flow: &str,
) -> RouterResult<()> {
    if let AuthenticationType::ApiKey { merchant_id, .. } = auth {
        if collection_merchants.contains(merchant_id) && !is_collection_flow(flow) {
            return Err(ApiErrorResponse::AccessForbidden {
                resource: flow.to_owned(),
            }
            .into());
        }
    }
    Ok(())
}

/// Collection callers cannot supply per-request credentials or override the
/// platform's channel selection through the payment API. Inspect request fields,
/// not arbitrary nested metadata. Arrays represent tuple payloads in API wrappers.
pub fn check_request_overrides(
    collection_merchants: &HashSet<id_type::MerchantId>,
    auth: &AuthenticationType,
    payload: &serde_json::Value,
) -> RouterResult<()> {
    if !matches!(
        auth,
        AuthenticationType::ApiKey { .. }
            | AuthenticationType::MerchantJwt { .. }
            | AuthenticationType::MerchantJwtWithProfileId { .. }
            | AuthenticationType::EmbeddedJwt { .. }
            | AuthenticationType::PublishableKey { .. }
            | AuthenticationType::SdkAuthorization { .. }
    ) {
        return Ok(());
    }
    if auth
        .get_merchant_id()
        .is_some_and(|id| collection_merchants.contains(id))
        && has_channel_override(payload)
    {
        return Err(ApiErrorResponse::AccessForbidden {
            resource: "platform_managed_channel_selection".to_owned(),
        }
        .into());
    }
    Ok(())
}

fn has_channel_override(payload: &serde_json::Value) -> bool {
    match payload {
        serde_json::Value::Object(fields) => [
            "routing",
            "connector",
            "merchant_connector_details",
            "creds_identifier",
        ]
        .iter()
        .any(|field| fields.get(*field).is_some_and(|value| !value.is_null())),
        serde_json::Value::Array(items) => items.iter().any(has_channel_override),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn service_keys_allow_collection_but_not_management() {
        for flow in ["PaymentsCreate", "PaymentsRetrieve", "RefundsCreate"] {
            assert!(is_collection_flow(flow));
        }
        for flow in [
            "MerchantConnectorsCreate",
            "MerchantConnectorsList",
            "RoutingCreateConfig",
            "ApiKeyCreate",
            "OrganizationCreate",
            "PlatformPaymentsList",
            "FutureManagementAPI",
        ] {
            assert!(!is_collection_flow(flow));
        }
    }

    #[test]
    fn platform_credentials_are_not_restricted() {
        assert!(check_service_key_flow(
            &HashSet::new(),
            &AuthenticationType::AdminApiKey,
            "MerchantConnectorsCreate"
        )
        .is_ok());
    }

    #[test]
    fn restriction_is_scoped_to_the_authenticated_merchant() {
        let merchant_id = id_type::MerchantId::try_from(std::borrow::Cow::from("collection_a"))
            .expect("merchant id");
        let key = AuthenticationType::ApiKey {
            merchant_id: merchant_id.clone(),
            key_id: id_type::ApiKeyId::generate_key_id("test"),
        };
        let restricted = HashSet::from([merchant_id]);
        assert!(check_service_key_flow(&restricted, &key, "PaymentsCreate").is_ok());
        assert!(check_service_key_flow(&restricted, &key, "MerchantConnectorsCreate").is_err());
        assert!(check_service_key_flow(&restricted, &key, "FutureManagementAPI").is_err());
        assert!(check_service_key_flow(&HashSet::new(), &key, "MerchantConnectorsCreate").is_ok());
        assert!(check_service_key_flow(
            &restricted,
            &AuthenticationType::AdminApiKey,
            "MerchantConnectorsCreate"
        )
        .is_ok());
    }

    #[test]
    fn collection_requests_cannot_override_channels() {
        use serde_json::json;
        let merchant_id = id_type::MerchantId::try_from(std::borrow::Cow::from("collection_a"))
            .expect("merchant id");
        let auth = AuthenticationType::ApiKey {
            merchant_id: merchant_id.clone(),
            key_id: id_type::ApiKeyId::generate_key_id("test"),
        };
        let webhook = AuthenticationType::WebhookAuth {
            merchant_id: merchant_id.clone(),
        };
        let registry = HashSet::from([merchant_id]);
        assert!(check_request_overrides(
            &registry,
            &webhook,
            &json!({"connector": "upstream notification"})
        )
        .is_ok());
        for payload in [
            json!({"routing": {"type": "single"}}),
            json!({"connector": ["stripe"]}),
            json!(["payment_id", {"merchant_connector_details": {}}]),
        ] {
            assert!(check_request_overrides(&registry, &auth, &payload).is_err());
            assert!(
                check_request_overrides(&registry, &AuthenticationType::AdminApiKey, &payload)
                    .is_ok()
            );
        }
        assert!(check_request_overrides(
            &registry,
            &auth,
            &json!({"amount": 100, "routing": null,
                "metadata": {"connector": "business metadata"}})
        )
        .is_ok());
    }
}
