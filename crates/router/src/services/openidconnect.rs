use base64::Engine;
use common_utils::errors::ErrorSwitch;
use error_stack::ResultExt;
use external_services::http_client::client;
use hyperswitch_masking::{ExposeInterface, PeekInterface, Secret};
use oidc::TokenResponse;
use openidconnect::{self as oidc, core as oidc_core};
use redis_interface::RedisConnectionWithContext;
use storage_impl::errors::ApiClientError;

use crate::{
    consts,
    core::errors::{UserErrors, UserResult},
    routes::SessionState,
    types::domain::user::UserEmail,
};

pub async fn get_authorization_url(
    state: SessionState,
    redirect_url: String,
    redirect_state: Secret<String>,
    base_url: Secret<String>,
    client_id: Secret<String>,
    use_pkce: bool,
    request_logto_roles: bool,
) -> UserResult<url::Url> {
    let discovery_document = get_discovery_document(base_url, &state).await?;

    let pkce = use_pkce.then(oidc::PkceCodeChallenge::new_random_sha256);
    let client = get_oidc_core_client(discovery_document, client_id, None, redirect_url)?;
    let mut authorization = client
        .authorize_url(
            oidc_core::CoreAuthenticationFlow::AuthorizationCode,
            || oidc::CsrfToken::new(redirect_state.expose()),
            oidc::Nonce::new_random,
        )
        .add_scope(oidc::Scope::new("email".to_string()));
    if request_logto_roles {
        authorization = authorization
            .add_scope(oidc::Scope::new("roles".to_string()))
            .add_scope(oidc::Scope::new(
                "urn:logto:scope:organization_roles".to_string(),
            ));
    }
    if let Some((challenge, _)) = &pkce {
        authorization = authorization.set_pkce_challenge(challenge.clone());
    }
    let (auth_url, csrf_token, nonce) = authorization.url();

    // Save csrf & nonce as key value respectively
    let key = get_oidc_redis_key(csrf_token.secret());
    get_redis_connection_for_global_tenant(&state)?
        .set_key_with_expiry(
            &key.clone().into(),
            nonce.secret(),
            consts::user::REDIS_SSO_TTL,
        )
        .await
        .change_context(UserErrors::InternalServerError)
        .attach_printable("Failed to save csrf-nonce in redis")?;

    if let Some((_, verifier)) = pkce {
        get_redis_connection_for_global_tenant(&state)?
            .set_key_with_expiry(
                &format!("{key}_PKCE").into(),
                verifier.secret(),
                consts::user::REDIS_SSO_TTL,
            )
            .await
            .change_context(UserErrors::InternalServerError)?;
    }
    Ok(auth_url)
}

pub struct OidcIdentity {
    pub email: UserEmail,
    pub subject: String,
    pub roles: Vec<String>,
    pub organization_roles: Vec<String>,
}

pub async fn get_identity_from_oidc_provider(
    state: &SessionState,
    redirect_url: String,
    redirect_state: Secret<String>,
    base_url: Secret<String>,
    client_id: Secret<String>,
    authorization_code: Secret<String>,
    client_secret: Secret<String>,
    require_verified_identity: bool,
    parse_logto_roles: bool,
) -> UserResult<OidcIdentity> {
    let nonce = get_nonce_from_redis(state, &redirect_state).await?;
    let discovery_document = get_discovery_document(base_url, state).await?;
    let client = get_oidc_core_client(
        discovery_document,
        client_id,
        Some(client_secret),
        redirect_url,
    )?;

    // Send request to OpenId provider with authorization code
    let key = get_oidc_redis_key(redirect_state.peek());
    let mut exchange =
        client.exchange_code(oidc::AuthorizationCode::new(authorization_code.expose()));
    if require_verified_identity {
        let verifier = get_redis_connection_for_global_tenant(state)?
            .get_key::<Option<String>>(&format!("{key}_PKCE").into())
            .await
            .change_context(UserErrors::InternalServerError)?
            .ok_or(UserErrors::SSOFailed)?;
        exchange = exchange.set_pkce_verifier(oidc::PkceCodeVerifier::new(verifier));
    }
    let token_response = exchange
        .request_async(|req| get_oidc_reqwest_client(state, req))
        .await
        .map_err(|e| match e {
            oidc::RequestTokenError::ServerResponse(resp)
                if resp.error() == &oidc_core::CoreErrorResponseType::InvalidGrant =>
            {
                UserErrors::SSOFailed
            }
            _ => UserErrors::InternalServerError,
        })
        .attach_printable("Failed to exchange code and fetch oidc token")?;

    // Fetch id token from response
    let id_token = token_response
        .id_token()
        .ok_or(UserErrors::InternalServerError)
        .attach_printable("Id Token not provided in token response")?;

    // Verify id token
    let id_token_claims = id_token
        .claims(&client.id_token_verifier(), &nonce)
        .change_context(UserErrors::InternalServerError)
        .attach_printable("Failed to verify id token")?;

    if require_verified_identity && id_token_claims.email_verified() != Some(true) {
        return Err(UserErrors::SSOFailed.into());
    }

    // Read extra claims only after the OIDC library has verified signature,
    // issuer, audience, expiry and nonce above. Missing/malformed claims fail closed.
    let (roles, organization_roles) = if parse_logto_roles {
        let jwt = id_token.to_string();
        let payload = jwt.split('.').nth(1).ok_or(UserErrors::SSOFailed)?;
        let decoded = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .decode(payload)
            .map_err(|_| UserErrors::SSOFailed)?;
        let claims: serde_json::Value =
            serde_json::from_slice(&decoded).map_err(|_| UserErrors::SSOFailed)?;
        let read_roles = |name: &str| -> UserResult<Vec<String>> {
            claims
                .get(name)
                .and_then(serde_json::Value::as_array)
                .ok_or(UserErrors::SSOFailed)?
                .iter()
                .map(|role| {
                    role.as_str()
                        .map(str::to_owned)
                        .ok_or(UserErrors::SSOFailed.into())
                })
                .collect()
        };
        (read_roles("roles")?, read_roles("organization_roles")?)
    } else {
        (Vec::new(), Vec::new())
    };

    // Get email from token
    let email_from_token = id_token_claims
        .email()
        .map(|email| email.to_string())
        .ok_or(UserErrors::InternalServerError)
        .attach_printable("OpenID Provider Didnt provide email")?;

    let email = UserEmail::new(Secret::new(email_from_token))
        .change_context(UserErrors::InternalServerError)
        .attach_printable("Failed to create email type")?;
    let connection = get_redis_connection_for_global_tenant(state)?;
    connection
        .delete_key(&key.into())
        .await
        .change_context(UserErrors::InternalServerError)?;
    connection
        .delete_key(&format!("{}_PKCE", get_oidc_redis_key(redirect_state.peek())).into())
        .await
        .change_context(UserErrors::InternalServerError)?;
    Ok(OidcIdentity {
        email,
        subject: id_token_claims.subject().as_str().to_owned(),
        roles,
        organization_roles,
    })
}

// TODO: Cache Discovery Document
async fn get_discovery_document(
    base_url: Secret<String>,
    state: &SessionState,
) -> UserResult<oidc_core::CoreProviderMetadata> {
    let issuer_url =
        oidc::IssuerUrl::new(base_url.expose()).change_context(UserErrors::InternalServerError)?;
    oidc_core::CoreProviderMetadata::discover_async(issuer_url, |req| {
        get_oidc_reqwest_client(state, req)
    })
    .await
    .change_context(UserErrors::InternalServerError)
}

fn get_oidc_core_client(
    discovery_document: oidc_core::CoreProviderMetadata,
    client_id: Secret<String>,
    client_secret: Option<Secret<String>>,
    redirect_url: String,
) -> UserResult<oidc_core::CoreClient> {
    let client_id = oidc::ClientId::new(client_id.expose());
    let client_secret = client_secret.map(|secret| oidc::ClientSecret::new(secret.expose()));
    let redirect_url = oidc::RedirectUrl::new(redirect_url)
        .change_context(UserErrors::InternalServerError)
        .attach_printable("Error creating redirect URL type")?;

    Ok(
        oidc_core::CoreClient::from_provider_metadata(discovery_document, client_id, client_secret)
            .set_redirect_uri(redirect_url),
    )
}

async fn get_nonce_from_redis(
    state: &SessionState,
    redirect_state: &Secret<String>,
) -> UserResult<oidc::Nonce> {
    let redis_connection = get_redis_connection_for_global_tenant(state)?;
    let redirect_state = redirect_state.clone().expose();
    let key = get_oidc_redis_key(&redirect_state);
    redis_connection
        .get_key::<Option<String>>(&key.into())
        .await
        .change_context(UserErrors::InternalServerError)
        .attach_printable("Error Fetching CSRF from redis")?
        .map(oidc::Nonce::new)
        .ok_or(UserErrors::SSOFailed)
        .attach_printable("Cannot find csrf in redis. Csrf invalid or expired")
}

async fn get_oidc_reqwest_client(
    state: &SessionState,
    request: oidc::HttpRequest,
) -> Result<oidc::HttpResponse, ApiClientError> {
    let client = client::create_client(&state.conf.proxy, None, None, None)
        .map_err(|e| e.current_context().switch())?;

    let mut request_builder = client
        .request(request.method, request.url)
        .body(request.body);
    for (name, value) in &request.headers {
        request_builder = request_builder.header(name.as_str(), value.as_bytes());
    }

    let request = request_builder
        .build()
        .map_err(|_| ApiClientError::ClientConstructionFailed)?;
    let response = client
        .execute(request)
        .await
        .map_err(|_| ApiClientError::RequestNotSent("OpenIDConnect".to_string()))?;

    Ok(oidc::HttpResponse {
        status_code: response.status(),
        headers: response.headers().to_owned(),
        body: response
            .bytes()
            .await
            .map_err(|_| ApiClientError::ResponseDecodingFailed)?
            .to_vec(),
    })
}

fn get_oidc_redis_key(csrf: &str) -> String {
    format!("{}OIDC_{}", consts::user::REDIS_SSO_PREFIX, csrf)
}

fn get_redis_connection_for_global_tenant(
    state: &SessionState,
) -> UserResult<RedisConnectionWithContext> {
    state
        .global_store
        .get_redis_conn()
        .change_context(UserErrors::InternalServerError)
        .attach_printable("Failed to get redis connection")
}
