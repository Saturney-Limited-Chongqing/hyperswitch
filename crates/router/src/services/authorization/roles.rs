use std::collections::HashSet;

use common_enums::{
    EntityType, MerchantProductType, PermissionGroup, PermissionScope, Resource, RoleScope,
};
use common_utils::{errors::CustomResult, id_type};

use super::{permission_groups::PermissionGroupExt, permissions::Permission};
use crate::{core::errors, routes::SessionState};

pub mod predefined_roles;

#[derive(Clone, serde::Serialize, serde::Deserialize, Debug)]
pub struct RoleInfo {
    role_id: String,
    role_name: String,
    groups: Vec<PermissionGroup>,
    scope: RoleScope,
    entity_type: EntityType,
    is_invitable: bool,
    is_deletable: bool,
    is_updatable: bool,
    is_internal: bool,
    product_type_filter: Option<MerchantProductType>,
}

impl RoleInfo {
    pub fn is_collection_merchant(&self) -> bool {
        matches!(
            self.role_id.as_str(),
            crate::consts::user_role::ROLE_ID_COLLECTION_MERCHANT_ADMIN
                | crate::consts::user_role::ROLE_ID_COLLECTION_MERCHANT_VIEW_ONLY
        )
    }

    /// Collection administrators may delegate only the collection dashboard roles.
    /// Entity hierarchy alone is insufficient: the legacy merchant admin is at the
    /// same entity level but can manage connectors and routing.
    pub fn can_delegate_role(&self, target: &Self) -> bool {
        !self.is_collection_merchant()
            || (self.role_id == crate::consts::user_role::ROLE_ID_COLLECTION_MERCHANT_ADMIN
                && target.is_collection_merchant())
    }

    fn collection_resource_allowed(resource: Resource) -> bool {
        matches!(
            resource,
            Resource::Payment
                | Resource::PaymentLink
                | Resource::Refund
                | Resource::Customer
                | Resource::Dispute
                | Resource::Report
                | Resource::Analytics
                | Resource::User
                | Resource::ApiKey
                | Resource::WebhookEvent
                | Resource::Account
        )
    }

    pub fn get_role_id(&self) -> &str {
        &self.role_id
    }

    pub fn get_role_name(&self) -> &str {
        &self.role_name
    }

    pub fn get_permission_groups(&self) -> Vec<PermissionGroup> {
        self.groups
            .iter()
            .flat_map(|group| group.accessible_groups())
            .filter(|group| {
                !self.is_collection_merchant() || *group != PermissionGroup::ConnectorsView
            })
            .collect::<HashSet<_>>()
            .into_iter()
            .collect()
    }

    pub fn get_scope(&self) -> RoleScope {
        self.scope
    }

    pub fn get_entity_type(&self) -> EntityType {
        self.entity_type
    }

    pub fn is_invitable(&self) -> bool {
        self.is_invitable
    }

    pub fn is_deletable(&self) -> bool {
        self.is_deletable
    }

    pub fn is_internal(&self) -> bool {
        self.is_internal
    }

    pub fn is_updatable(&self) -> bool {
        self.is_updatable
    }

    pub fn get_product_type_filter(&self) -> Option<MerchantProductType> {
        match self.entity_type {
            EntityType::Organization | EntityType::Tenant => None,
            EntityType::Merchant | EntityType::Profile => Some(
                self.product_type_filter
                    .unwrap_or(MerchantProductType::Orchestration),
            ),
        }
    }

    pub fn get_resources_set(&self) -> HashSet<Resource> {
        self.get_permission_groups()
            .iter()
            .flat_map(|group| group.resources())
            .filter(|resource| {
                !self.is_collection_merchant() || Self::collection_resource_allowed(*resource)
            })
            .collect()
    }

    pub fn check_permission_exists(&self, required_permission: Permission) -> bool {
        // OperationsManage also includes Account. Collection merchants must not
        // obtain account administration through that inherited resource.
        let collection_allowed = !self.is_collection_merchant()
            || (Self::collection_resource_allowed(required_permission.resource())
                && !(required_permission.resource() == Resource::Account
                    && required_permission.scope() == PermissionScope::Write));
        collection_allowed
            && required_permission.entity_type() <= self.entity_type
            && self.get_permission_groups().iter().any(|group| {
                required_permission.scope() <= group.scope()
                    && group.resources().contains(&required_permission.resource())
            })
    }

    pub fn from_predefined_roles(role_id: &str) -> Option<Self> {
        predefined_roles::PREDEFINED_ROLES.get(role_id).cloned()
    }

    pub async fn from_role_id_in_lineage(
        state: &SessionState,
        role_id: &str,
        merchant_id: &id_type::MerchantId,
        org_id: &id_type::OrganizationId,
        profile_id: &id_type::ProfileId,
        tenant_id: &id_type::TenantId,
    ) -> CustomResult<Self, errors::StorageError> {
        if let Some(role) = predefined_roles::PREDEFINED_ROLES.get(role_id) {
            Ok(role.clone())
        } else {
            state
                .global_store
                .find_role_by_role_id_in_lineage(
                    role_id,
                    merchant_id,
                    org_id,
                    profile_id,
                    tenant_id,
                )
                .await
                .map(Self::from)
        }
    }

    // TODO: To evaluate whether we can omit org_id and tenant_id for this function
    pub async fn from_role_id_org_id_tenant_id(
        state: &SessionState,
        role_id: &str,
        org_id: &id_type::OrganizationId,
        tenant_id: &id_type::TenantId,
    ) -> CustomResult<Self, errors::StorageError> {
        if let Some(role) = predefined_roles::PREDEFINED_ROLES.get(role_id) {
            Ok(role.clone())
        } else {
            state
                .global_store
                .find_by_role_id_org_id_tenant_id(role_id, org_id, tenant_id)
                .await
                .map(Self::from)
        }
    }
}

impl From<diesel_models::role::Role> for RoleInfo {
    fn from(role: diesel_models::role::Role) -> Self {
        Self {
            role_id: role.role_id,
            role_name: role.role_name,
            groups: role.groups,
            scope: role.scope,
            entity_type: role.entity_type,
            is_invitable: true,
            is_deletable: true,
            is_updatable: true,
            is_internal: false,
            product_type_filter: role.merchant_product_type,
        }
    }
}

#[cfg(test)]
mod collection_tests {
    use super::*;
    use crate::consts::user_role;

    fn collection_admin() -> RoleInfo {
        RoleInfo::from_predefined_roles(user_role::ROLE_ID_COLLECTION_MERCHANT_ADMIN)
            .expect("collection role must exist")
    }

    #[test]
    fn collection_admin_cannot_manage_platform_resources() {
        let role = collection_admin();
        assert_eq!(role.get_scope(), RoleScope::Merchant);
        for permission in [
            Permission::MerchantPaymentRead,
            Permission::MerchantPaymentWrite,
            Permission::MerchantRefundWrite,
            Permission::MerchantUserWrite,
            Permission::MerchantApiKeyWrite,
        ] {
            assert!(role.check_permission_exists(permission));
        }
        for permission in [
            Permission::MerchantConnectorRead,
            Permission::MerchantConnectorWrite,
            Permission::ProfileConnectorRead,
            Permission::ProfileConnectorWrite,
            Permission::ProfileRoutingWrite,
            Permission::MerchantRoutingRead,
            Permission::MerchantRoutingWrite,
            Permission::MerchantAccountWrite,
            Permission::OrganizationAccountRead,
            Permission::TenantAccountRead,
            Permission::MerchantCloneConnectorWrite,
        ] {
            assert!(!role.check_permission_exists(permission));
        }
        for resource in [Resource::Connector, Resource::Routing, Resource::Payout] {
            assert!(!role.get_resources_set().contains(&resource));
        }
        assert!(!role
            .get_permission_groups()
            .contains(&PermissionGroup::ConnectorsView));
    }

    #[test]
    fn collection_viewer_has_no_write_or_delegation_access() {
        let role =
            RoleInfo::from_predefined_roles(user_role::ROLE_ID_COLLECTION_MERCHANT_VIEW_ONLY)
                .expect("collection role must exist");
        assert!(role.check_permission_exists(Permission::MerchantPaymentRead));
        for permission in [
            Permission::MerchantPaymentWrite,
            Permission::MerchantRefundWrite,
            Permission::MerchantUserWrite,
            Permission::MerchantApiKeyWrite,
        ] {
            assert!(!role.check_permission_exists(permission));
        }
        assert!(!role.can_delegate_role(&collection_admin()));
    }

    #[test]
    fn same_entity_level_does_not_allow_privilege_escalation() {
        let admin = collection_admin();
        let legacy_admin = RoleInfo::from_predefined_roles(user_role::ROLE_ID_MERCHANT_ADMIN)
            .expect("legacy role must exist");
        assert_eq!(admin.get_entity_type(), legacy_admin.get_entity_type());
        assert!(!admin.can_delegate_role(&legacy_admin));
        let mut custom_admin = legacy_admin.clone();
        custom_admin.role_id = "role_custom_admin".to_owned();
        assert!(!admin.can_delegate_role(&custom_admin));
        assert!(admin.can_delegate_role(&admin));
        // Existing platform administration continues to work.
        let platform = RoleInfo::from_predefined_roles(common_utils::consts::ROLE_ID_TENANT_ADMIN)
            .expect("platform role must exist");
        assert!(platform.can_delegate_role(&admin));
        assert!(platform.check_permission_exists(Permission::MerchantConnectorWrite));
    }

    #[test]
    fn role_cache_round_trip_preserves_collection_policy() {
        let cached = serde_json::to_string(&collection_admin()).expect("serialize role");
        let role: RoleInfo = serde_json::from_str(&cached).expect("deserialize role");
        assert!(!role.check_permission_exists(Permission::MerchantConnectorWrite));
        assert!(!role.check_permission_exists(Permission::MerchantAccountWrite));
    }
}
