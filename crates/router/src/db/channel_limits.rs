//! Durable channel quota operations. PostgreSQL serializes reservations on the pool row.
use crate::{
    connection,
    core::errors::{ApiErrorResponse, RouterResult},
    db::{KafkaStore, MockDb, Store},
};
use async_bb8_diesel::AsyncRunQueryDsl;
use error_stack::ResultExt;
use serde_json::Value;

#[async_trait::async_trait]
pub trait ChannelLimitInterface {
    async fn channel_limit_command(&self, operation: &str, payload: Value) -> RouterResult<Value>;
}
#[derive(diesel::QueryableByName)]
struct CommandResult {
    #[diesel(sql_type = diesel::sql_types::Text)]
    result: String,
}
#[async_trait::async_trait]
impl ChannelLimitInterface for Store {
    async fn channel_limit_command(&self, operation: &str, payload: Value) -> RouterResult<Value> {
        let conn = connection::pg_connection_write(self)
            .await
            .change_context(ApiErrorResponse::InternalServerError)?;
        let row: CommandResult =
            diesel::sql_query("SELECT channel_limit_command($1, $2::jsonb)::text AS result")
                .bind::<diesel::sql_types::Text, _>(operation.to_owned())
                .bind::<diesel::sql_types::Text, _>(payload.to_string())
                .get_result_async(conn.raw_connection())
                .await
                .change_context(ApiErrorResponse::InternalServerError)?;
        serde_json::from_str(&row.result).change_context(ApiErrorResponse::InternalServerError)
    }
}
#[async_trait::async_trait]
impl ChannelLimitInterface for KafkaStore {
    async fn channel_limit_command(&self, operation: &str, payload: Value) -> RouterResult<Value> {
        self.diesel_store
            .channel_limit_command(operation, payload)
            .await
    }
}
#[async_trait::async_trait]
impl ChannelLimitInterface for MockDb {
    async fn channel_limit_command(&self, _: &str, _: Value) -> RouterResult<Value> {
        Err(ApiErrorResponse::InternalServerError.into())
    }
}
