use crate::services::authorization::permissions::Permission;
use actix_web::{web, HttpRequest, Responder};
use router_env::{instrument, tracing, Flow};

use super::app::AppState;
use crate::{
    core::{api_locking, configs},
    services::{api, authentication as auth},
    types::api as api_types,
};

#[cfg(feature = "v1")]
const ADMIN_API_AUTH: auth::AdminApiAuth = auth::AdminApiAuth;
#[cfg(feature = "v2")]
const ADMIN_API_AUTH: auth::V2AdminApiAuth = auth::V2AdminApiAuth;

#[instrument(skip_all, fields(flow = ?Flow::CreateConfigKey))]
pub async fn config_key_create(
    state: web::Data<AppState>,
    req: HttpRequest,
    json_payload: web::Json<api_types::Config>,
) -> impl Responder {
    let flow = Flow::CreateConfigKey;
    let payload = json_payload.into_inner();

    api::server_wrap(
        flow,
        state,
        &req,
        payload,
        |state, _, data, _| configs::set_config(state, data),
        &ADMIN_API_AUTH,
        api_locking::LockAction::NotApplicable,
    )
    .await
}
#[instrument(skip_all, fields(flow = ?Flow::ConfigKeyFetch))]
pub async fn config_key_retrieve(
    state: web::Data<AppState>,
    req: HttpRequest,
    path: web::Path<String>,
) -> impl Responder {
    let flow = Flow::ConfigKeyFetch;
    let key = path.into_inner();

    api::server_wrap(
        flow,
        state,
        &req,
        &key,
        |state, _, key, _| configs::read_config(state, key),
        &auth::InternalApiKeyAuth(ADMIN_API_AUTH),
        api_locking::LockAction::NotApplicable,
    )
    .await
}
#[instrument(skip_all, fields(flow = ?Flow::ConfigKeyUpdate))]
pub async fn config_key_update(
    state: web::Data<AppState>,
    req: HttpRequest,
    path: web::Path<String>,
    json_payload: web::Json<api_types::ConfigUpdate>,
) -> impl Responder {
    let flow = Flow::ConfigKeyUpdate;
    let mut payload = json_payload.into_inner();
    let key = path.into_inner();
    payload.key = key;

    api::server_wrap(
        flow,
        state,
        &req,
        &payload,
        |state, _, payload, _| configs::update_config(state, payload),
        &ADMIN_API_AUTH,
        api_locking::LockAction::NotApplicable,
    )
    .await
}

#[instrument(skip_all, fields(flow = ?Flow::ConfigKeyDelete))]
pub async fn config_key_delete(
    state: web::Data<AppState>,
    req: HttpRequest,
    path: web::Path<String>,
) -> impl Responder {
    let flow = Flow::ConfigKeyDelete;
    let key = path.into_inner();

    api::server_wrap(
        flow,
        state,
        &req,
        key,
        |state, _, key, _| configs::config_delete(state, key),
        &ADMIN_API_AUTH,
        api_locking::LockAction::NotApplicable,
    )
    .await
}

/// Platform-only management. Merchant keys and dashboard roles cannot mutate quotas.
#[cfg(feature = "v1")]
pub async fn channel_limits_list(state: web::Data<AppState>, req: HttpRequest) -> impl Responder {
    api::server_wrap(
        Flow::ConfigKeyFetch,
        state,
        &req,
        (),
        |state, _, _, _| async move {
            let value = state
                .store
                .channel_limit_command("list", serde_json::json!({}))
                .await?;
            Ok(crate::services::ApplicationResponse::Json(value))
        },
        auth::auth_type(
            &ADMIN_API_AUTH,
            &auth::JWTAuth {
                permission: Permission::TenantAccountWrite,
                allow_connected: false,
                allow_platform: true,
            },
            req.headers(),
        ),
        api_locking::LockAction::NotApplicable,
    )
    .await
}
#[cfg(feature = "v1")]
pub async fn channel_limits_set(
    state: web::Data<AppState>,
    req: HttpRequest,
    payload: web::Json<serde_json::Value>,
) -> impl Responder {
    api::server_wrap(
        Flow::ConfigKeyUpdate,
        state,
        &req,
        payload.into_inner(),
        |state, _, data, _| async move {
            for key in [
                "merchant_id",
                "merchant_connector_id",
                "pool_id",
                "currency",
                "timezone",
            ] {
                if !data
                    .get(key)
                    .and_then(serde_json::Value::as_str)
                    .is_some_and(|s| !s.is_empty() && s.len() <= 128)
                {
                    return Err(crate::core::errors::ApiErrorResponse::InvalidRequestData {
                        message: format!("Invalid {key}"),
                    }
                    .into());
                }
            }
            if !data
                .get("daily_limit")
                .and_then(serde_json::Value::as_i64)
                .is_some_and(|n| n >= 0)
            {
                return Err(crate::core::errors::ApiErrorResponse::InvalidRequestData {
                    message: "daily_limit must be a nonnegative minor-unit integer".to_owned(),
                }
                .into());
            }
            let value = state.store.channel_limit_command("set", data).await?;
            Ok(crate::services::ApplicationResponse::Json(value))
        },
        auth::auth_type(
            &ADMIN_API_AUTH,
            &auth::JWTAuth {
                permission: Permission::TenantAccountWrite,
                allow_connected: false,
                allow_platform: true,
            },
            req.headers(),
        ),
        api_locking::LockAction::NotApplicable,
    )
    .await
}

#[cfg(feature = "v1")]
pub async fn channel_limits_resolve(
    state: web::Data<AppState>,
    req: HttpRequest,
    payload: web::Json<serde_json::Value>,
) -> impl Responder {
    api::server_wrap(Flow::ConfigKeyUpdate,state,&req,payload.into_inner(),|state,_,data,_| async move {
        if !matches!(data.get("state").and_then(serde_json::Value::as_str),Some("committed" | "released"))
            || !data.get("reason").and_then(serde_json::Value::as_str).is_some_and(|v|v.trim().len()>=10)
            || ["merchant_id","merchant_connector_id","attempt_id"].iter().any(|key|!data.get(*key).and_then(serde_json::Value::as_str).is_some_and(|v|!v.is_empty()))
        {
            return Err(crate::core::errors::ApiErrorResponse::InvalidRequestData { message:"Resolution requires merchant, channel, attempt, state and reconciliation evidence".to_owned() }.into());
        }
        let value=state.store.channel_limit_command("resolve",data).await?;
        Ok(crate::services::ApplicationResponse::Json(value))
    },auth::auth_type(&ADMIN_API_AUTH,&auth::JWTAuth {
        permission: Permission::TenantAccountWrite,
        allow_connected: false,allow_platform:true,
    },req.headers()),api_locking::LockAction::NotApplicable).await
}
