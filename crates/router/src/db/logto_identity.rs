use api_models::user::LogtoRoleBinding;
use async_bb8_diesel::AsyncRunQueryDsl;
use diesel::sql_types::Text;
use error_stack::ResultExt;

use crate::{
    connection,
    core::errors::{UserErrors, UserResult},
    db::{KafkaStore, MockDb, Store},
};

#[derive(diesel::QueryableByName)]
struct BindingRow {
    #[diesel(sql_type = Text)]
    user_id: String,
    #[diesel(sql_type = Text)]
    role_id: String,
    #[diesel(sql_type = diesel::sql_types::Nullable<Text>)]
    organization_id: Option<String>,
    #[diesel(sql_type = diesel::sql_types::Nullable<Text>)]
    merchant_id: Option<String>,
    #[diesel(sql_type = Text)]
    status: String,
}

#[async_trait::async_trait]
pub trait LogtoIdentityInterface {
    async fn find_ready_logto_binding(&self, subject: &str)
        -> UserResult<Option<LogtoRoleBinding>>;
}

#[async_trait::async_trait]
impl LogtoIdentityInterface for Store {
    async fn find_ready_logto_binding(
        &self,
        subject: &str,
    ) -> UserResult<Option<LogtoRoleBinding>> {
        let conn = connection::pg_connection_read(self)
            .await
            .change_context(UserErrors::InternalServerError)?;
        let rows: Vec<BindingRow> = diesel::sql_query(
            "SELECT local_user_id AS user_id, role_id, logto_organization_id AS organization_id, \
             local_merchant_id AS merchant_id, status FROM logto_identity_binding \
             WHERE logto_subject = $1 LIMIT 2",
        )
        .bind::<Text, _>(subject.to_owned())
        .get_results_async(conn.raw_connection())
        .await
        .change_context(UserErrors::InternalServerError)?;
        if rows.len() > 1 {
            return Err(UserErrors::SSOFailed.into());
        }
        if rows.first().is_some_and(|row| row.status != "ready") {
            return Err(UserErrors::SSOFailed.into());
        }
        Ok(rows.into_iter().next().map(|row| LogtoRoleBinding {
            user_id: row.user_id,
            role_id: row.role_id,
            organization_id: row.organization_id,
            merchant_id: row.merchant_id,
        }))
    }
}

#[async_trait::async_trait]
impl LogtoIdentityInterface for KafkaStore {
    async fn find_ready_logto_binding(
        &self,
        subject: &str,
    ) -> UserResult<Option<LogtoRoleBinding>> {
        self.diesel_store.find_ready_logto_binding(subject).await
    }
}

#[async_trait::async_trait]
impl LogtoIdentityInterface for MockDb {
    async fn find_ready_logto_binding(&self, _: &str) -> UserResult<Option<LogtoRoleBinding>> {
        Err(UserErrors::InternalServerError.into())
    }
}
